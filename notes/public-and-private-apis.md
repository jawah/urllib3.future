# Public and private APIs

## Public APIs

- `urllib3.request()`
- `urllib3.PoolManager`
- `urllib3.ProxyManager`
- `urllib3.HTTPConnectionPool`
- `urllib3.HTTPSConnectionPool`
- `urllib3.HTTPResponse`
- `urllib3.HTTPHeaderDict`
- `urllib3.filepost`
- `urllib3.fields`
- `urllib3.exceptions`
- `urllib3.contrib.socks`
- `urllib3.contrib.pyopenssl`
- `urllib3.util`

Only public way to configure proxies is through `ProxyManager`?

## Private APIs

- `urllib3.connection`
- `urllib3.connection.HTTPConnection`
- `urllib3.connection.HTTPSConnection`
- `urllib3.util.*` (submodules)
