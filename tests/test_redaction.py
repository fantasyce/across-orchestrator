from across_orchestrator.redaction import redact_sensitive_value


def test_public_redaction_preserves_task_ids_while_redacting_real_secrets():
    task_id = "task-gca-final-public-worker-0121-1788104489"
    secret_value = "sk-" + "example-secret-value"

    public = redact_sensitive_value({
        "task_id": task_id,
        "message": f"credential {secret_value} must stay private",
    })

    assert public["task_id"] == task_id
    assert public["message"] == "credential [redacted] must stay private"
