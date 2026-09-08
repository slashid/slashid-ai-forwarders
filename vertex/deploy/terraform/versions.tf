terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0"
    }
    http = {
      source  = "hashicorp/http"
      version = ">= 3.4"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}
