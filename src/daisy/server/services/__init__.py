"""Per-concern service modules holding the server's domain logic, split out of the old
monolith. Each imports the shared state, database, and models layers (and, where a domain
genuinely depends on another, that sibling service); none imports the boot or asgi module,
so the routes and the boot composition root both depend on services, not the reverse."""
