from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0016_fixedpricelisting_original_price'),
    ]

    operations = [
        # --- Chat: allow store-level (item-less) conversations ---
        migrations.AlterField(
            model_name='chat',
            name='item',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='chats',
                to='core.item',
            ),
        ),
        migrations.AddField(
            model_name='chat',
            name='store',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='chats',
                to='core.userstore',
            ),
        ),
        # --- StoreFollow ---
        migrations.CreateModel(
            name='StoreFollow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('follower', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='following_stores',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('store', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='followers',
                    to='core.userstore',
                )),
            ],
            options={
                'ordering': ['-created_at'],
                'unique_together': {('follower', 'store')},
            },
        ),
        # --- StoreCoupon ---
        migrations.CreateModel(
            name='StoreCoupon',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=40)),
                ('amount', models.DecimalField(
                    decimal_places=2, default=Decimal('0.00'), max_digits=12,
                    validators=[django.core.validators.MinValueValidator(0)],
                )),
                ('min_order_amount', models.DecimalField(
                    decimal_places=2, default=Decimal('0.00'), max_digits=12,
                    validators=[django.core.validators.MinValueValidator(0)],
                )),
                ('description', models.CharField(blank=True, max_length=200)),
                ('is_active', models.BooleanField(default=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('store', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='coupons',
                    to='core.userstore',
                )),
            ],
            options={
                'ordering': ['-created_at'],
                'unique_together': {('store', 'code')},
            },
        ),
        # --- CouponCollection ---
        migrations.CreateModel(
            name='CouponCollection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('collected_at', models.DateTimeField(auto_now_add=True)),
                ('coupon', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='collections',
                    to='core.storecoupon',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='collected_coupons',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-collected_at'],
                'unique_together': {('user', 'coupon')},
            },
        ),
    ]
