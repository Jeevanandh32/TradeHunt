# TradeHunt lab host: one VM runs the whole containerlab topology.
# Multicast never leaves the VM, so the VPC's lack of multicast support doesn't matter.

data "google_project" "lab" {
  project_id = var.project_id
}

resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "iam" {
  service            = "iam.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "iap" {
  service            = "iap.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_network" "lab" {
  name                    = "tradehunt-vpc"
  auto_create_subnetworks = false

  depends_on = [google_project_service.compute]
}

resource "google_compute_subnetwork" "lab" {
  name          = "tradehunt-subnet"
  network       = google_compute_network.lab.id
  region        = var.region
  ip_cidr_range = "10.100.0.0/24"
}

# SSH only from Google's IAP range; no other inbound traffic is allowed.
resource "google_compute_firewall" "iap_ssh" {
  name          = "tradehunt-allow-iap-ssh"
  network       = google_compute_network.lab.id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["tradehunt-lab"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# The VM needs no Google API access, so it gets an identity with no roles.
resource "google_service_account" "lab" {
  depends_on = [google_project_service.iam]

  account_id   = "tradehunt-lab"
  display_name = "TradeHunt lab VM"
}

resource "google_compute_instance" "lab" {
  name         = "tradehunt-lab"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["tradehunt-lab"]

  boot_disk {
    initialize_params {
      image = var.image
      size  = var.disk_size_gb
      type  = var.disk_type
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.lab.id
    # Ephemeral public IP for outbound downloads (packages, market data); inbound is blocked by the firewall.
    access_config {}
  }

  service_account {
    email  = google_service_account.lab.email
    scopes = []
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  metadata_startup_script = file("${path.module}/startup.sh")

  depends_on = [google_project_service.iap]
}

resource "google_project_service" "billingbudgets" {
  count              = var.billing_account == "" ? 0 : 1
  service            = "billingbudgets.googleapis.com"
  disable_on_destroy = false
}

resource "google_billing_budget" "lab" {
  count           = var.billing_account == "" ? 0 : 1
  billing_account = var.billing_account
  display_name    = "tradehunt-lab-budget"

  budget_filter {
    # The Budgets API stores the project by number, so use it to avoid a diff on every plan.
    projects = ["projects/${data.google_project.lab.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }

  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.9 }
  threshold_rules { threshold_percent = 1.0 }

  depends_on = [google_project_service.billingbudgets]
}
