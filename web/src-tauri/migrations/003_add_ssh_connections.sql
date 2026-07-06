ALTER TABLE connections ADD COLUMN ssh_host_alias TEXT;
ALTER TABLE connections ADD COLUMN ssh_host_name TEXT;
ALTER TABLE connections ADD COLUMN ssh_user TEXT;
ALTER TABLE connections ADD COLUMN ssh_port INTEGER;
ALTER TABLE connections ADD COLUMN ssh_identity_file TEXT;
ALTER TABLE connections ADD COLUMN ssh_local_port INTEGER;
ALTER TABLE connections ADD COLUMN ssh_remote_port INTEGER;
ALTER TABLE connections ADD COLUMN ssh_context TEXT;
