output "database_id" {
  value = aws_db_instance.app.identifier
}

output "database_host" {
  value = aws_db_instance.app.address
}

output "database_port" {
  value = aws_db_instance.app.port
}

output "database_name" {
  value = aws_db_instance.app.db_name
}

output "master_secret_arn" {
  description = "RDS-managed bootstrap credential ARN, never supplied to the app."
  value       = aws_db_instance.app.master_user_secret[0].secret_arn
}

output "runtime_secret_name" {
  value = aws_secretsmanager_secret.runtime.name
}

output "github_secret_name" {
  value = aws_secretsmanager_secret.github.name
}

output "database_security_group_id" {
  value = aws_security_group.database.id
}
