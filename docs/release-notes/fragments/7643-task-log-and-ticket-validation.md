## Dashboard log streaming and ticket validation

Dashboard task streams read new log lines on a worker thread so other requests can proceed during file reads. Repeated ticket validation reuses the validator for the current schema definition, including its date checks.
