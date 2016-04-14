# Copyright (C) 2016 SignalFx, Inc. All rights reserved.

import json
import sseclient

from . import messages


class ComputationExecutionError(Exception):

    def __init__(self, code, message=None):
        self._code = code
        self._message = message

    @property
    def code(self):
        return self._code

    @property
    def message(self):
        return self._message

    def __str__(self):
        return self._message


class Computation(object):
    """A live handle to a running SignalFlow computation.

    A computation object allows access to the computation's output in real-time
    via the stream() method. It automatically analyzes the incoming stream
    messages and presents the computation's data and metadata output in a
    conveniently consumable way.
    """

    _CONTROL_MESSAGE_TYPE = 'CONTROL'
    _METADATA_MESSAGE_TYPE = 'METADATA'
    _DATA_MESSAGE_TYPE = 'DATA'
    _EVENT_MESSAGE_TYPE = 'EVENT'

    def __init__(self, conn, url, program, params):
        self._id = None
        self._conn = conn
        self._url = url
        self._program = program
        self._params = params

        self._progress = 0
        self._complete = False
        self._resolution = None
        self._metadata = {}
        self._last_logical_ts = None

        r = self._conn.request_encode_url(
                'POST', self._url,
                fields=self._params, body=self._program,
                preload_content=False)
        if r.status != 200:
            try:
                if r.headers['Content-Type'] == 'application/json':
                    raise ComputationExecutionError(**json.loads(r.read()))
                raise ComputationExecutionError(r.status)
            finally:
                r.close()

        self._events = sseclient.SSEClient(r)

    @property
    def id(self):
        return self._id

    @property
    def program(self):
        return self._program

    @property
    def progress(self):
        return self._progress

    @property
    def resolution(self):
        return self._resolution

    def is_complete(self):
        return self._complete

    def get_known_tsids(self):
        return sorted(self._metadata.keys())

    def get_metadata(self, tsid):
        """Return the full metadata object for the given timeseries (by its
        ID), if available."""
        return self._metadata.get(tsid)

    def stream(self):
        """Iterate over the messages from the computation's output.

        Control and metadata messages are intercepted and interpreted to
        enhance this Computation's object knowledge of the computation's
        context. Data and event messages are yielded back to the caller as a
        generator.
        """

        # TODO(mpetazzoni): automatically re-issue the query with Last-Event-ID
        # header on httplib.IncompleteRead exceptions.

        last_data_batch = None
        for event in self._events.events():
            message = messages.StreamMessage.decode(event)
            if isinstance(message, messages.EndOfChannelMessage):
                self._complete = True
                break

            if isinstance(message, messages.JobProgressMessage):
                self._progress = message.progress
                continue

            # Intercept metadata messages to accumulate received metadata.
            # TODO(mpetazzoni): this can accumulate metadata without bounds if
            # a computation has a high rate of member churn.
            if isinstance(message, messages.MetadataMessage):
                self._metadata[message.tsid] = message.properties
                continue

            if isinstance(message, messages.DigestMessage):
                self._process_message_digest(message.digest)
                continue

            # Automatically and immediately yield events.
            if isinstance(message, messages.EventMessage):
                yield message

            # Accumulate data messages and release them when we have received
            # all batches for the same logical timestamp.
            if isinstance(message, messages.DataMessage):
                if not last_data_batch:
                    last_data_batch = message
                elif message.logical_timestamp_ms == \
                        last_data_batch.logical_timestamp_ms:
                    last_data_batch.add_data(message.data)
                else:
                    to_yield, last_data_batch = last_data_batch, message
                    self._last_logical_ts = to_yield.logical_timestamp_ms
                    yield to_yield

        # Yield last batch, even if potentially incomplete.
        if last_data_batch:
            yield last_data_batch

    def close(self):
        """Manually close this computation's output if its stream wasn't
        entirely consumed."""
        self._events.close()

    def _process_message_digest(self, digest):
        """Process a message digest sent by the computation.

        Message digests contain information about the running computation that
        we can extract to provide more details about what the computation is
        doing, any warnings that we might want to surface to the user, etc.
        """
        for message in digest:
            # Extract the output resolution from the appropriate message, if
            # it's present.
            if message['messageCode'] == 'JOB_RUNNING_RESOLUTION':
                self._resolution = message['jsonPayload']['resolutionMs']