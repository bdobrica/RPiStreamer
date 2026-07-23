variable "VERSION" {
  default = "0.1.0.dev0"
}

variable "REVISION" {
  default = "unknown"
}

variable "CREATED" {
  default = "unknown"
}

group "default" {
  targets = ["indexer", "nginx"]
}

target "_common" {
  platforms = ["linux/amd64", "linux/arm64"]
  args = {
    VERSION = VERSION
    REVISION = REVISION
    CREATED = CREATED
  }
}

target "indexer" {
  inherits = ["_common"]
  context = "."
  dockerfile = "Dockerfile"
  tags = ["rpi-streamer-indexer:${VERSION}"]
}

target "nginx" {
  inherits = ["_common"]
  context = "."
  dockerfile = "deployment/container/nginx.Dockerfile"
  tags = ["rpi-streamer-nginx:${VERSION}"]
}
