locals {
  identifier                 = "tmprl-dem-cld-${var.project_name}"
  secret_name                = "tmprl-dem-cld/${var.project_name}/runtime"
  allowed_security_group_ids = setunion(var.eks_security_group_ids, var.operator_security_group_ids)
}

data "aws_vpc" "platform" {
  id = var.vpc_id
}

data "aws_subnet" "private" {
  for_each = var.private_subnet_ids
  id       = each.value
}

data "aws_security_group" "clients" {
  for_each = local.allowed_security_group_ids
  id       = each.value
}

data "aws_rds_engine_version" "postgres" {
  engine                 = "postgres"
  parameter_group_family = "postgres17"
  version                = var.postgres_engine_version
  latest                 = var.postgres_engine_version == null
}

resource "aws_db_subnet_group" "app" {
  name       = local.identifier
  subnet_ids = var.private_subnet_ids
  lifecycle {
    precondition {
      condition = alltrue([
        for subnet in data.aws_subnet.private :
        subnet.vpc_id == var.vpc_id && !subnet.map_public_ip_on_launch && try(subnet.tags["kubernetes.io/role/internal-elb"], "") == "1"
      ]) && length(toset([for subnet in data.aws_subnet.private : subnet.availability_zone])) >= 2
      error_message = "Use existing platform private subnets (internal-elb=1) in the VPC, spanning two AZs, with public-IP assignment disabled."
    }
  }
}

resource "aws_security_group" "database" {
  name        = "${local.identifier}-db"
  description = "Dedicated private PostgreSQL for ${var.project_name}"
  vpc_id      = data.aws_vpc.platform.id
}

resource "aws_vpc_security_group_ingress_rule" "postgres" {
  for_each                     = local.allowed_security_group_ids
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "PostgreSQL from approved demo or management host"
  lifecycle {
    precondition {
      condition     = data.aws_security_group.clients[each.value].vpc_id == var.vpc_id
      error_message = "All approved client security groups must belong to the platform VPC."
    }
  }
}

resource "aws_db_parameter_group" "app" {
  name   = local.identifier
  family = "postgres17"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

resource "aws_db_instance" "app" {
  identifier                      = local.identifier
  engine                          = "postgres"
  engine_version                  = data.aws_rds_engine_version.postgres.version_actual
  instance_class                  = var.db_instance_class
  allocated_storage               = 20
  max_allocated_storage           = 100
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_name                         = var.db_name
  username                        = "box_admin"
  manage_master_user_password     = true
  db_subnet_group_name            = aws_db_subnet_group.app.name
  vpc_security_group_ids          = [aws_security_group.database.id]
  parameter_group_name            = aws_db_parameter_group.app.name
  publicly_accessible             = false
  multi_az                        = var.multi_az
  backup_retention_period         = 7
  backup_window                   = "09:00-10:00"
  maintenance_window              = "sun:10:00-sun:11:00"
  copy_tags_to_snapshot           = true
  auto_minor_version_upgrade      = true
  allow_major_version_upgrade     = false
  apply_immediately               = false
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = var.final_snapshot_identifier
  delete_automated_backups        = false
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  lifecycle {
    prevent_destroy = true
  }
}

# Values are generated or entered outside Terraform; no passwords enter state.
resource "aws_secretsmanager_secret" "runtime" {
  name                    = local.secret_name
  description             = "App database URL, webhook signing secret, and executor bearer token; populated by deploy/bootstrap-secrets.py"
  recovery_window_in_days = 30
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret" "github" {
  name                    = "tmprl-dem-cld/${var.project_name}/github"
  description             = "Fine-grained GitHub sandbox token; entered outside Terraform"
  recovery_window_in_days = 30
  lifecycle {
    prevent_destroy = true
  }
}
