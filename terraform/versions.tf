terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  # Billing Budgets API calls made with user credentials need a quota project.
  user_project_override = true
  billing_project       = var.project_id
}
