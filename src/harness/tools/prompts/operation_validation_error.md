The `edit_file` tool received an operation that doesn't match the expected schema.

**Operation index:** `{{ index }}`
**Operation type:** `{{ type }}`
**Problem field:** `{{ field }}`

**What was expected:** {{ expected }}

**What was received:** `{{ actual }}`

**Full operation that failed:**
```json
{{ operation_json }}
```

Fix the operation and resubmit. The valid operation types are: {{ valid_types }}.
