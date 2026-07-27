from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/chat/(?P<chat_id>\w+)/$', consumers.ChatConsumer.as_asgi()),
    re_path(r'ws/notifications/$', consumers.NotificationConsumer.as_asgi()),
    re_path(r'ws/auction/(?P<auction_id>\w+)/$', consumers.AuctionConsumer.as_asgi()),  # NEW
    re_path(r'ws/live/(?P<session_id>\w+)/$', consumers.LiveSessionConsumer.as_asgi()),
]