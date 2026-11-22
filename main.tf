terraform {
  required_version = ">= 1.5, < 2.0"
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "= 0.244.0"
    }
  }
}
variable "folder_id" { type = string }
variable "cloud_id" { type = string }
variable "bucket_name" {
  type     = string
  nullable = false
  validation {
    condition     = can(regex("^vvot17-[a-z0-9][a-z0-9-]{0,54}[a-z0-9]$", var.bucket_name))
    error_message = "Использовать глобальное уникальное имя бакета vvot17-"
  }
}
provider "yandex" {
  cloud_id  = var.cloud_id
  folder_id = var.folder_id
}
locals {
  name = "vvot17-parcel"
  roles = {
    ingress  = ["ymq.writer", "ydb.viewer", "storage.viewer"]
    delivery = ["ymq.reader", "functions.functionInvoker"]
    packer   = ["storage.uploader", "ydb.editor"]
    queues   = ["ymq.admin"]
  }
  bindings = merge([for actor, roles in local.roles : {
    for role in roles : "${actor}/${role}" => { actor = actor, role = role }
  }]...)
}
resource "yandex_iam_service_account" "identity" {
  for_each = local.roles
  name     = "${local.name}-${each.key}"
}
resource "yandex_resourcemanager_folder_iam_member" "grant" {
  for_each  = local.bindings
  role      = each.value.role
  folder_id = var.folder_id
  member    = "serviceAccount:${yandex_iam_service_account.identity[each.value.actor].id}"
}
resource "yandex_iam_service_account_static_access_key" "signature" {
  for_each           = toset(["queues", "packer"])
  service_account_id = yandex_iam_service_account.identity[each.key].id
  depends_on         = [yandex_resourcemanager_folder_iam_member.grant]
}
resource "yandex_message_queue" "failed" {
  name                      = "${local.name}-failed"
  access_key                = yandex_iam_service_account_static_access_key.signature["queues"].access_key
  secret_key                = yandex_iam_service_account_static_access_key.signature["queues"].secret_key
  message_retention_seconds = 1209600
}
resource "yandex_message_queue" "incoming" {
  name                       = "${local.name}-incoming"
  access_key                 = yandex_iam_service_account_static_access_key.signature["queues"].access_key
  secret_key                 = yandex_iam_service_account_static_access_key.signature["queues"].secret_key
  visibility_timeout_seconds = 900
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 86400
  redrive_policy = jsonencode({
    deadLetterTargetArn = yandex_message_queue.failed.arn
    maxReceiveCount     = 1
  })
}
resource "yandex_storage_bucket" "parcels" {
  bucket        = var.bucket_name
  folder_id     = var.folder_id
  max_size      = 10 * 1024 * 1024 * 1024
  force_destroy = true
  anonymous_access_flags {
    read        = false
    list        = false
    config_read = false
  }
}
resource "yandex_ydb_database_serverless" "index" {
  name                = "${local.name}-index"
  location_id         = "ru-central1"
  deletion_protection = false
  serverless_database {
    enable_throttling_rcu_limit = true
    throttling_rcu_limit        = 10
    storage_size_limit          = 1
  }
}
resource "terraform_data" "catalog" {
  triggers_replace = [yandex_ydb_database_serverless.index.id, filesha256("tools/catalog.py")]
  provisioner "local-exec" {
    command = "python3 tools/catalog.py"
    environment = {
      ENDPOINT = yandex_ydb_database_serverless.index.document_api_endpoint
    }
  }
}
locals {
  revision = sha256(join("", [for f in ["worker/job.py", "worker/requirements.txt"] : filesha256(f)]))
}
resource "terraform_data" "bundle" {
  input            = ".terraform/worker.zip"
  triggers_replace = [local.revision]
  provisioner "local-exec" {
    command = "python3 -m zipfile -c .terraform/worker.zip worker/job.py worker/requirements.txt"
  }
}
resource "yandex_function" "pack" {
  name               = "${local.name}-pack"
  runtime            = "python312"
  memory             = 256
  entrypoint         = "job.handle"
  execution_timeout  = "600"
  user_hash          = local.revision
  service_account_id = yandex_iam_service_account.identity["packer"].id
  environment = {
    PACK_BUCKET = yandex_storage_bucket.parcels.bucket
    PACK_DB     = yandex_ydb_database_serverless.index.document_api_endpoint
    PACK_ACCESS = yandex_iam_service_account_static_access_key.signature["packer"].access_key
    PACK_SECRET = yandex_iam_service_account_static_access_key.signature["packer"].secret_key
  }
  content { zip_filename = terraform_data.bundle.output }
  depends_on = [terraform_data.bundle, terraform_data.catalog]
}
resource "yandex_function_trigger" "consume" {
  name = "${local.name}-consume"
  function {
    id                 = yandex_function.pack.id
    service_account_id = yandex_iam_service_account.identity["delivery"].id
  }
  message_queue {
    queue_id           = yandex_message_queue.incoming.arn
    service_account_id = yandex_iam_service_account.identity["delivery"].id
    batch_size         = 1
    batch_cutoff       = 0
  }
}
resource "yandex_api_gateway" "public" {
  name = "${local.name}-http"
  spec = templatefile("openapi.yaml", {
    actor  = yandex_iam_service_account.identity["ingress"].id
    queue  = yandex_message_queue.incoming.id
    folder = var.folder_id
    db     = yandex_ydb_database_serverless.index.database_path
    bucket = yandex_storage_bucket.parcels.bucket
  })
  depends_on = [yandex_function_trigger.consume]
}
output "api_url" { value = "https://${yandex_api_gateway.public.domain}" }
output "worker_id" { value = yandex_function.pack.id }
output "failed_queue" { value = yandex_message_queue.failed.id }
output "queue_test_credentials" {
  sensitive = true
  value = {
    AWS_ACCESS_KEY_ID     = yandex_iam_service_account_static_access_key.signature["queues"].access_key
    AWS_SECRET_ACCESS_KEY = yandex_iam_service_account_static_access_key.signature["queues"].secret_key
  }
}
