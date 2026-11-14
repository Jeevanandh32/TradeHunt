variable "project_id" {
  description = "GCP project that hosts the lab VM"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-east4"
}

variable "zone" {
  description = "GCP zone for the lab VM"
  type        = string
  default     = "us-east4-a"
}

variable "machine_type" {
  description = "VM size: 4 cEOS switches, 3 hosts and monitoring fit in 16 GB (Axion ARM, matches the cEOSarm image)"
  type        = string
  default     = "c4a-standard-4"
}

variable "image" {
  description = "Boot image; must match the machine type's CPU (arm64 for c4a, amd64 for e2)"
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2404-lts-arm64"
}

variable "disk_size_gb" {
  description = "Boot disk size; room for the cEOS image, containers and Nasdaq ITCH slices"
  type        = number
  default     = 100
}

variable "disk_type" {
  description = "Boot disk type; C4A (Axion) supports Hyperdisk only"
  type        = string
  default     = "hyperdisk-balanced"
}

variable "billing_account" {
  description = "Billing account ID for the budget alert; leave empty to skip the budget"
  type        = string
  default     = ""
}

variable "budget_usd" {
  description = "Monthly budget alert amount in USD"
  type        = number
  default     = 50
}
