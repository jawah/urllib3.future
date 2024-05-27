# Copyright 2022 Akamai Technologies, Inc
# Largely rewritten in 2023 for urllib3-future
# Copyright 2024 Ahmed Tahri
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HTTPErrorCodes:
    """
    Provides commonly used error codes for a version of the HTTP protocol.

    Each HTTP version uses different error codes,
    so we provide a few error codes for common use cases.
    """

    # Generic error when a peer violated our expectations.
    protocol_error: int

    # Generic error when something went wrong at our side.
    internal_error: int

    # The TCP connection established in response to a CONNECT request
    # was reset or abnormally closed.
    connect_error: int
