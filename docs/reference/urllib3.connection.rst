Connections
===========

Applications normally use connection pools. Direct connection objects expose
the lower-level request lifecycle and require the caller to manage reuse and
cleanup. See :doc:`urllib3.connectionpool` for the pooled APIs.

.. automodule:: urllib3.connection

.. autoclass:: urllib3.connection.HTTPConnection
    :members:
    :exclude-members: putrequest
    :show-inheritance:

.. autoclass:: urllib3.connection.HTTPSConnection
    :members:
    :show-inheritance:

.. autoclass:: urllib3._async.connection.AsyncHTTPConnection
    :members:
    :exclude-members: putrequest
    :show-inheritance:

.. autoclass:: urllib3._async.connection.AsyncHTTPSConnection
    :members:
    :show-inheritance:

.. autoclass:: urllib3._typing.ProxyConfig
    :members:
    :show-inheritance:
