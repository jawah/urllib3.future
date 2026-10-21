Response and Decoders
=====================

See :ref:`response-lifecycle` for streaming cleanup and shutdown behavior, and
:ref:`response-read-semantics` for differences between sync and async reads.

Response
--------

.. autoclass:: urllib3.response.HTTPResponse
    :members:
    :undoc-members:
    :show-inheritance:
    :inherited-members: json

    .. autoattribute:: auto_close
    .. autoattribute:: status
    .. autoattribute:: headers

.. autoclass:: urllib3.AsyncHTTPResponse
    :members:
    :undoc-members:
    :show-inheritance:
    :inherited-members: json

    .. autoattribute:: auto_close
    .. autoattribute:: status
    .. autoattribute:: headers

Decoders
--------

Decoder classes are used for transforming compressed HTTP bodies
using the ``Content-Encoding`` into their uncompressed binary
representation.

.. autoclass:: urllib3.response.BrotliDecoder
.. autoclass:: urllib3.response.DeflateDecoder
.. autoclass:: urllib3.response.GzipDecoder
.. autoclass:: urllib3.response.ZstdDecoder
.. autoclass:: urllib3.response.MultiDecoder
