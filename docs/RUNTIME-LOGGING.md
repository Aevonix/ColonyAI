# Runtime logs

`pacomind start` uses Python's `RotatingFileHandler`, with a 20 MiB threshold
and four numbered archives. A private instance writes
`<instance>/service/sidecar.log`. `PACOMIND_LOG_PATH`, `PACOMIND_LOG_MAX_BYTES`
and `PACOMIND_LOG_BACKUPS` select another path, size threshold or archive count
at process startup. Log files and the writer declaration have mode `0600`.

Managed instance services capture Python stdout and stderr in the same sink.
Applications that embed the sidecar can call
`pacomind.runtime_logging.configure_runtime_logging(path, redirect_stdio=True)`
before importing the ASGI application. Direct Uvicorn launches can set
`PACOMIND_RUNTIME_LOGGING=1`; the shipped service template selects this path.
One process owns each log. The handler does not coordinate multiple server
workers writing the same file.

Log timestamps use UTC. Only successful GET requests to these exact routes
are suppressed, and only when their measured response-header latency is less
than one second:

- `/v1/host/health`
- `/v1/host/transport/ingress/receipts`
- `/v1/host/memory/sources/erasures`
- `/v1/host/queue/jobs/pending`
- `/v1/host/queue/stats`

Failed, redirected, slow, unmeasured and other requests remain visible.
Successful receipt-query parameters are marked `<omitted>` when that request
is logged. An access record's latency measures response-header emission,
not completion of a streamed response body.

A formatted record is capped at 65,536 characters plus a truncation marker;
source records and request data are unchanged. A single formatted record can
exceed the rotation threshold, and UTF-8 characters may occupy multiple bytes.
Python stdio lines are emitted in bounded chunks. Raw native writes to operating
system descriptors, and output before logging is configured, remain under the
service manager's existing output handling.

The handler writes `<log>.runtime.json` at startup. The existing native review
reader reads this bounded declaration beside a registered log and reports its
writer PID, configured size/count and current numbered archive sizes. This
declares how that process configured logging; it does not prove the process is
still alive. Missing or invalid declarations remain explicitly unavailable.
The reader retains its existing task ownership and registered-path checks.

The operational volume review measures top-level `*.log` files in the selected
writer directory. Recognized numbered, `.bak` and `.gz` archives are counted
separately and do not trigger the 100 MiB review threshold. Archive subdirectories
are not scanned. This threshold is a reason to inspect log volume, not evidence
of low free disk space or a faulty polling loop.

When adopting rotation with an existing large log, stop its writer, preserve
the old file outside numbered retention, and then start the new writer. An
atomic rename on the same filesystem preserves its inode and bytes. Do this
before the new handler writes: otherwise ordinary rotation will move the old
file into the finite archive set. Verify the selected process PID against its
startup declaration and observe fresh output in the new file. No periodic
truncation, restart job or additional logging service is required.
