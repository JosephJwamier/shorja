"""What an uploaded picture has to be, in one place.

`ImageField` on a model checks nothing by itself — `objects.create(image=f)`
stored an SVG with a script in it under the name its sender gave it — and no
field anywhere limited the size.
"""
from django.core.exceptions import ValidationError

MAX_BYTES = 12 * 1024 * 1024
# A small file can still decode to something enormous.
MAX_PIXELS = 60_000_000
MAX_IMAGES_PER_REQUEST = 20

# Pillow's name -> the only extensions we write. The web server picks the
# Content-Type from the extension, so it comes from the decoded image and
# never from the sender's filename.
EXTENSIONS = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp', 'GIF': 'gif'}


def validate_image(upload):
    """Raises ValidationError unless `upload` is a real picture of a sane size.
    Renames it to match what it is. A file already stored is left alone."""
    if getattr(upload, '_committed', False):
        return
    from PIL import Image

    if upload.size > MAX_BYTES:
        raise ValidationError('الصورة %d ميغابايت كحد أقصى' % (MAX_BYTES // (1024 * 1024)))
    source = getattr(upload, 'file', upload)
    try:
        source.seek(0)
        # Only our formats reach a decoder at all: the rest are where most of
        # Pillow's holes are found.
        with Image.open(source, formats=tuple(EXTENSIONS)) as image:
            kind, (width, height) = image.format, image.size
            if width * height > MAX_PIXELS:
                raise ValidationError('أبعاد الصورة كبيرة جداً')
            image.verify()
    except ValidationError:
        raise
    except Exception:
        raise ValidationError('الملف ليس صورة صالحة')
    finally:
        source.seek(0)
    if kind not in EXTENSIONS:
        raise ValidationError('الصورة JPEG أو PNG أو WebP أو GIF')
    stem = (upload.name or 'image').rsplit('/', 1)[-1].rsplit('.', 1)[0] or 'image'
    upload.name = '%s.%s' % (stem[:80], EXTENSIONS[kind])
