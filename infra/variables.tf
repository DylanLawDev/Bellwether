variable "project_id" {
  type        = string
  description = "GCP project ID to deploy into"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "bucket_name" {
  type        = string
  description = "globally-unique GCS bronze bucket name"
}

variable "database_url" {
  type        = string
  sensitive   = true
  description = "Neon (or other external Postgres) connection string — stored in Secret Manager"
}

variable "image" {
  type        = string
  description = "container image for the API service + worker job"
  # placeholder until T14's pipeline pushes the real image
  default = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "obs_bucket" {
  type    = string
  default = "hour"
}
