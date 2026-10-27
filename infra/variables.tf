variable "region" {
  type    = string
  default = "us-west-1"
}

variable "aws_account_id" {
  description = "Expected AWS account; prevents deploying with an unintended profile."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit account ID."
  }
}

variable "project_name" {
  description = "Matches DemoProject.metadata.name."
  type        = string
  default     = "box-agent-sdlc"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,24}$", var.project_name))
    error_message = "Use a lowercase project name of 2–25 characters."
  }
}

variable "vpc_id" {
  description = "Existing demo-platform VPC; this stack never changes its network."
  type        = string
}

variable "private_subnet_ids" {
  description = "Existing private platform subnets in at least two availability zones."
  type        = set(string)
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Supply at least two private subnets."
  }
}

variable "eks_security_group_ids" {
  description = "Existing EKS cluster/node security groups used by demo pods, for all active colors."
  type        = set(string)
  validation {
    condition     = length(var.eks_security_group_ids) > 0
    error_message = "Supply at least one existing EKS security group."
  }
}

variable "operator_security_group_ids" {
  description = "Optional existing private management-host security groups for database bootstrap. Never use public CIDRs."
  type        = set(string)
  default     = []
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "multi_az" {
  description = "Retain synchronous standby for recovery during an AZ failure."
  type        = bool
  default     = true
}

variable "db_name" {
  type    = string
  default = "box_agent_sdlc"
  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{1,62}$", var.db_name))
    error_message = "Use a PostgreSQL identifier with lowercase letters, digits, or underscores."
  }
}

variable "postgres_engine_version" {
  description = "Optional exact available PostgreSQL 17 version. Null resolves the latest available 17 minor in this region."
  type        = string
  default     = null
  validation {
    condition     = var.postgres_engine_version == null || can(regex("^17\\.[0-9]+", var.postgres_engine_version))
    error_message = "This stack supports PostgreSQL 17."
  }
}

variable "final_snapshot_identifier" {
  description = "Unique retained final snapshot name; set a new name before intentionally replacing a previous database."
  type        = string
  default     = "tmprl-dem-cld-box-agent-sdlc-final"
}
