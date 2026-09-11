# Apsimo sidecar

Apsimo provides persistent source memory, relevant context and shared work for
a personal agent using locally configured models. This package runs the sidecar
and supplies the `apsimo` command.

Native Hermes attachment also requires the companion `apsimo-hermes` package
and an existing supported Hermes installation. The guided local setup uses one
OpenAI-compatible chat endpoint and SQLite; graph and vector dependencies are
optional.

See the [public setup guide](https://github.com/Aevonix/ApsimoAGI/blob/main/docs/LOCAL-HERMES-SETUP.md)
for installation, private identity setup, supported runtime and operation.
The [project README](https://github.com/Aevonix/ApsimoAGI/blob/main/README.md)
describes current capabilities and their limits. Personal configuration,
credentials and hardware adapters belong in the private deployment.

Licensed under the included MIT license. Optional dependencies retain their
own licenses.
