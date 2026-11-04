-- Who am I: how the login was done and which service it reached. A check of the Oracle connection, no bind variables:
--   uv run pcs_oracle_portable\oracle_db.py --sql queries\who.sql
select sys_context('USERENV','AUTHENTICATION_METHOD') as auth, sys_context('USERENV','SERVICE_NAME') as service from dual
