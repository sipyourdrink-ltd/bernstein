## The charm and continue adapters' contracts are found by adapter name

Contracts are looked up by adapter name, but two were filed under another
name: `crush.yaml` for the `charm` adapter and `continue_dev.yaml` for
`continue`. Every by-name lookup missed them. `bernstein adapters
contract-check charm` reported no contract, and volunteer mode reported both
adapters' auth basis as unknown although their contracts declare `api_key`.
The files are now `charm.yaml` and `continue.yaml`, and the contract-drift
matrix uses the same names. A unit test keeps every contract filed under a
registered adapter name.
