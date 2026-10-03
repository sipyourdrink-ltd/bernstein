# Edge Relay

This service acts as an edge relay, buffering and routing cluster requests to the appropriate backend shards. It uses a consistent hashing algorithm to deterministically map incoming requests to the correct shard based on a consistent hash of the node ID.

## Features

- **Consistent Hashing**: Implements a consistent hash ring with virtual nodes to ensure an even distribution of requests across shards and minimize key reassignments during shard additions or removals.
- **Request Routing**: Routes `GET` and `POST` requests for `/cluster/nodes/*` endpoints to the owning shard.
- **Node Registration**: For `POST /cluster/nodes`, it extracts the node ID from the request body to determine the owner shard.
- **Header Forwarding**: Forwards `Authorization` and `Content-Type` headers to the target shard.
- **421 Response Handling**: Passes through `421 Misdirected Request` responses from shards, allowing the client to be aware of redirection.

## Configuration

The relay's behavior is configured via environment variables, primarily `SHARD_MAP`, which defines the available shards and their URLs.

### Environment Variables

- `SHARD_MAP`: A JSON string representing an array of shard objects. Each object should have an `id` (string) and a `url` (string). Example:
  `'[{"id": "shard-0", "url": "http://localhost:8000"}, {"id": "shard-1", "url": "http://localhost:8001"}]'`
- `VNODE_COUNT`: The number of virtual nodes per physical shard. This value should match the backend cluster's configuration for consistent hashing. (Default to 128 if not specified).

## Development and Testing

The project includes unit tests for the `HashRing` implementation and the worker's routing logic.

To run tests:
```bash
node --test edge/relay/test/
```
