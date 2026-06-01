output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "bronze_bucket" {
  value = google_storage_bucket.bronze.name
}

output "artifact_repo" {
  value = google_artifact_registry_repository.repo.name
}
