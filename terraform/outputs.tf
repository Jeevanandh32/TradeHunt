output "ssh_command" {
  description = "SSH to the lab VM through IAP"
  value       = "gcloud compute ssh ${google_compute_instance.lab.name} --zone ${var.zone} --project ${var.project_id} --tunnel-through-iap"
}

output "grafana_tunnel" {
  description = "Forward Grafana to http://localhost:3000"
  value       = "gcloud compute ssh ${google_compute_instance.lab.name} --zone ${var.zone} --project ${var.project_id} --tunnel-through-iap -- -L 3000:localhost:3000"
}

output "stop_command" {
  description = "Stop the VM between sessions (disk is still billed)"
  value       = "gcloud compute instances stop ${google_compute_instance.lab.name} --zone ${var.zone} --project ${var.project_id}"
}
