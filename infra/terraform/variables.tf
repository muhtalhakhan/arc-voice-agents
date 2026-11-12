variable "project" {
  type = string
}

variable "env" {
  type = string
  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be dev or prod"
  }
}

variable "region" {
  type    = string
  default = "europe-west1"
}

variable "image" {
  type        = string
  description = "Initial image. Later deploys are done by CI/CD."
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "min_instances" {
  type    = number
  default = 0
}

variable "max_instances" {
  type    = number
  default = 5
}

variable "alert_channels" {
  type    = list(string)
  default = []
}
