"""A chat attachment has to be a picture, and is stored under a picture's name.

The old check read `content_type`, which the sender writes. An HTML page sent
as "image/png" got 201 and was stored as chat_attachments/x.html; Caddy serves
/media straight from disk with a type taken from the extension, so the page
ran as text/html on api.shorjeh.com, the admin's origin.

Now Pillow must decode the bytes, and the stored name is a random hex with an
extension taken from the decoded format, never from the sender's filename.
"""
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from core.models import Category, Chat, Item, Message, User

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media_in_tmp(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def chat():
    buyer = User.objects.create_user(username='att_buyer', password='pass12345')
    seller = User.objects.create_user(username='att_seller', password='pass12345')
    category, _ = Category.objects.get_or_create(name='attcat', slug='attcat')
    item = Item.objects.create(
        owner=seller, title='t', description='d', category=category,
        condition='new', quantity=1, status='active',
    )
    return Chat.objects.create(item=item, buyer=buyer, seller=seller)


def image_bytes(fmt):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def send(chat, upload):
    client = APIClient()
    client.force_authenticate(user=chat.buyer)
    return client.post(f'/api/chats/{chat.id}/send_message/',
                       {'attachment': upload}, format='multipart')


def stored_attachments(chat):
    return list(Message.objects.filter(chat=chat).exclude(attachment='')
                .values_list('attachment', flat=True))


@pytest.mark.parametrize('name, body, declared', [
    ('x.html', b'<script>alert(document.domain)</script>', 'image/png'),
    ('x.svg', b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', 'image/svg+xml'),
    ('x.png', b'not an image at all', 'image/png'),
])
def test_anything_pillow_cannot_decode_is_refused(chat, name, body, declared):
    response = send(chat, SimpleUploadedFile(name, body, content_type=declared))

    assert response.status_code == 400
    assert stored_attachments(chat) == []


def test_real_image_under_a_non_image_name_is_refused(chat):
    # Django's ImageField also rejects a non-image extension before Pillow
    # runs, so a valid picture called evil.html never gets stored at all.
    upload = SimpleUploadedFile('evil.html', image_bytes('PNG'), content_type='text/html')

    response = send(chat, upload)

    assert response.status_code == 400
    assert stored_attachments(chat) == []


def test_stored_name_is_random_not_the_senders(chat):
    upload = SimpleUploadedFile('../../admin<script>.png', image_bytes('PNG'),
                                content_type='image/png')

    response = send(chat, upload)

    assert response.status_code == 201
    [stored] = stored_attachments(chat)
    assert stored.startswith('chat_attachments/')
    assert stored.endswith('.png')
    assert 'admin' not in stored and 'script' not in stored


@pytest.mark.parametrize('fmt, extension', [('JPEG', '.jpg'), ('WEBP', '.webp'), ('GIF', '.gif')])
def test_the_extension_follows_the_decoded_format(chat, fmt, extension):
    upload = SimpleUploadedFile('photo.png', image_bytes(fmt), content_type='image/png')

    response = send(chat, upload)

    assert response.status_code == 201
    [stored] = stored_attachments(chat)
    assert stored.endswith(extension)


def test_a_text_message_still_needs_no_attachment(chat):
    client = APIClient()
    client.force_authenticate(user=chat.buyer)

    response = client.post(f'/api/chats/{chat.id}/send_message/',
                           {'content': 'مرحبا'}, format='multipart')

    assert response.status_code == 201
