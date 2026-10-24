Backends
========

These classes implement the transport/protocol layer used by connections.
Application code normally uses pools and responses; these entries are useful
when implementing adapters or inspecting the backend contract.

.. automodule:: urllib3.backend

.. autoclass:: urllib3.backend.BaseBackend
    :members:
    :show-inheritance:

.. autoclass:: urllib3.backend.hface.HfaceBackend
    :members:
    :show-inheritance:

.. autoclass:: urllib3.backend._async.AsyncBaseBackend
    :members:
    :show-inheritance:

.. autoclass:: urllib3.backend._async.AsyncHfaceBackend
    :members:
    :show-inheritance:

.. autoclass:: urllib3.backend.HttpVersion
    :members:

.. autoclass:: urllib3.backend.ResponsePromise
    :members:

.. autoclass:: urllib3.backend.QuicPreemptiveCacheType

.. autoclass:: urllib3.backend.ConnectionInfo
    :members:
