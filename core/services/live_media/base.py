"""Live-stream media providers.

Media never passes through Django. What Django does is hand a client the
credentials to publish or watch a session's stream, and — for a self-hosted
server — answer the server's "may this stream be published/played?" hooks.
Everything vendor-specific lives behind this interface so the session
model, the viewsets and the app only ever see one shape:

    {
      "provider": "srs",
      "role": "publisher" | "subscriber",
      "expires_in": 3600,
      ...provider fields (urls / tokens / ice servers)...
    }
"""


class MediaProviderError(Exception):
    pass


class LiveMediaProvider:
    name = ""

    def publish_credentials(self, session, user):
        """What the store owner's app needs to start broadcasting."""
        raise NotImplementedError

    def play_credentials(self, session, user):
        """What a viewer's app needs to watch."""
        raise NotImplementedError
