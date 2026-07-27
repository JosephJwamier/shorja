from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_store_follow_coupons_and_store_chat'),
    ]

    operations = [
        migrations.AddField(
            model_name='userstore',
            name='cover_image',
            field=models.ImageField(blank=True, null=True, upload_to='store_covers/'),
        ),
    ]
