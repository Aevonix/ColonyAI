# Retired effect workers

The initiative poller and Hermes queue worker are inert compatibility targets.
Their filenames remain so scheduled entries left behind by an upgrade stop
safely with exit code `78`; neither script performs network or queue work.

Remove existing scheduled entries after upgrade. New autonomous work is created
as an immutable `HermesToolActionIntentV1` by the general plugin and submitted to the
separate authenticated action mediator.
