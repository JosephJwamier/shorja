"""
Uploaded media (product/store images, receipts, chat attachments) and the
nightly mirror that now backs them up.

Until 2026-11-12 nothing backed these up at all: `MEDIA_ROOT` is a local
disk (the `media` docker volume) and `deploy/backup.sh` uploaded a mysqldump
and nothing else, so losing the server kept every row and lost every file
those rows point at. The fix mirrors the media tree into R2 nightly rather
than moving `STORAGES['default']` to R2 — see the header of backup.sh for
why serving stayed on local disk.

That choice makes the backup depend on two invariants that nothing else
enforces, and which a later refactor could silently break:

  1. uploads really land under MEDIA_ROOT — the one directory the mirror
     copies. Flip STORAGES['default'] to S3 and backup.sh would happily
     mirror an empty tree and exit 0, every night, for as long as it took
     anyone to notice.
  2. no field writes outside that tree. `upload_to` takes an arbitrary
     path; an absolute one puts files where the mirror never looks.

Pure unit tests: no network, no DB, no docker. The backup.sh assertions read
the script as text — crude, but it is the only thing standing between
`grep -c media deploy/backup.sh` going back to 0 and nobody noticing.
"""
import re
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import models
from django.test import override_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SH = REPO_ROOT / 'deploy' / 'backup.sh'
COMPOSE = REPO_ROOT / 'deploy' / 'docker-compose.yml'


class TestDefaultStorageIsTheMirroredDisk:
    """The mirror copies a directory. These assert it is the directory
    uploads actually go to."""

    def test_default_storage_is_the_local_filesystem(self):
        # Not a preference — a precondition. backup.sh mirrors a docker
        # volume; an object-storage backend would make it mirror nothing.
        assert settings.STORAGES['default']['BACKEND'] == \
            'django.core.files.storage.FileSystemStorage'
        assert isinstance(default_storage, FileSystemStorage)

    def test_media_root_is_the_volume_the_mirror_copies(self):
        # deploy/docker-compose.yml mounts the `media` volume at /app/media,
        # which is BASE_DIR/'media' inside the image (WORKDIR /app).
        assert Path(settings.MEDIA_ROOT) == Path(settings.BASE_DIR) / 'media'

    def test_uploaded_file_lands_under_media_root(self, tmp_path):
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            name = default_storage.save('item_images/probe.txt', ContentFile(b'x'))
            try:
                written = Path(default_storage.path(name))
                assert written.is_file()
                # The whole point: on disk, inside the mirrored tree.
                assert tmp_path in written.parents
            finally:
                default_storage.delete(name)

    def test_url_is_built_from_media_url_not_an_external_base(self, tmp_path):
        # Caddy serves /media/* from /srv/media on BOTH api.shorjeh.com and
        # shorjeh.com (the share pages' og:image). A URL that stopped
        # starting with MEDIA_URL would mean both handlers now miss.
        with override_settings(MEDIA_ROOT=str(tmp_path), MEDIA_URL='media/'):
            url = default_storage.url('item_images/probe.jpg')
        # Django prepends the script prefix, so settings' bare 'media/'
        # becomes '/media/…' — the leading slash is what makes Caddy's
        # `handle_path /media/*` match, so assert it rather than tolerate it.
        assert url == '/media/item_images/probe.jpg'


def _upload_to_fields():
    """Every concrete File/ImageField in the project with a string
    upload_to. Callable upload_to is skipped — there are none today, and a
    callable can't be checked without calling it."""
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, models.FileField) and isinstance(field.upload_to, str):
                yield f'{model._meta.label}.{field.name}', field.upload_to


class TestNothingEscapesTheMirroredTree:
    def test_every_upload_to_is_a_relative_path_inside_media_root(self):
        offenders = []
        for label, upload_to in _upload_to_fields():
            path = Path(upload_to)
            if path.is_absolute() or upload_to.startswith(('/', '\\')):
                offenders.append(f'{label}: absolute upload_to {upload_to!r}')
            elif '..' in path.parts:
                offenders.append(f'{label}: upload_to escapes MEDIA_ROOT {upload_to!r}')
            elif re.match(r'^[A-Za-z]:', upload_to):
                offenders.append(f'{label}: drive-qualified upload_to {upload_to!r}')
        assert not offenders, (
            'these fields write outside MEDIA_ROOT, so deploy/backup.sh never '
            'mirrors them: ' + '; '.join(offenders)
        )

    def test_the_known_upload_dirs_are_still_covered(self):
        # A sanity anchor: if this ever returns nothing, the scan above is
        # passing vacuously rather than proving anything.
        found = {upload_to for _, upload_to in _upload_to_fields()}
        assert 'item_images/' in found
        assert 'store_logos/' in found
        assert len(found) >= 8


@pytest.mark.skipif(not BACKUP_SH.exists(), reason='deploy/backup.sh not in this checkout')
class TestBackupScriptCoversMedia:
    """`grep -c media deploy/backup.sh` returning 0 is how the gap was found
    in the first place. These keep it from returning 0 again."""

    @staticmethod
    def _script():
        return BACKUP_SH.read_text(encoding='utf-8')

    def test_media_is_uploaded_to_the_backup_bucket(self):
        script = self._script()
        assert 'r2:shorjeh-backups/media/' in script
        assert '/app/media' in script

    def test_media_is_copied_not_synced(self):
        # sync propagates a local deletion into the only remaining copy, in
        # the same night. copy never deletes.
        script = self._script()
        assert re.search(r'^\s*media_rclone copy /app/media\b', script, re.M), \
            'the media mirror step is gone or renamed'
        assert not re.search(r'\bsync\b.*\bmedia\b', script), \
            'media must be copied, never synced — sync deletes'

    def test_media_is_never_pruned_by_age(self):
        # The 30-day rule is for dumps. A year-old product image is still
        # on display in the app.
        for line in self._script().splitlines():
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if '--min-age' in stripped:
                assert 'media' not in stripped, \
                    f'age-based deletion must not touch media: {stripped}'

    def test_volume_is_read_through_a_container_not_by_name(self):
        # /var/lib/docker is root-only and the deploy user is in `docker`,
        # not `sudo`; borrowing the volumes of a container that already
        # mounts it is what makes the job runnable as that user.
        assert '--volumes-from' in self._script()

    def test_failures_still_reach_sentry_for_the_media_part(self):
        script = self._script()
        assert 'PART="media"' in script
        assert 'trap on_exit EXIT' in script
        assert 'sentry_alert' in script

    def test_silently_empty_media_is_distinguished_from_a_new_install(self):
        # The analogue of the dump's 20 KB floor: an empty source is fine on
        # a fresh install and a failure once the mirror holds anything.
        assert re.search(
            r'\$\{SRC_FILES\}"?\s*-eq 0.*\$\{DST_FILES\}"?\s*-gt 0',
            self._script(),
        ), 'the empty-source guard is gone'


@pytest.mark.skipif(not COMPOSE.exists(), reason='deploy/docker-compose.yml not in this checkout')
class TestComposeStillExposesMediaToTheBackup:
    def test_web_mounts_the_media_volume_the_backup_borrows(self):
        # backup.sh takes `--volumes-from` the web (or worker) container.
        # If neither mounted `media` any more, the mirror would copy nothing.
        compose = COMPOSE.read_text(encoding='utf-8')
        assert compose.count('media:/app/media') >= 2, \
            'web and worker must both mount the media volume'
