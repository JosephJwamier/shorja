"""
ReSell Trust - Enhanced Core Django Models
P2P Marketplace with Auctions, Deposits, Representative System, and Advanced Item Transfer
"""
import uuid
from datetime import timedelta
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from decimal import Decimal
from django.core.exceptions import ValidationError
from .iraq_governorates import IRAQ_GOVERNORATE_CHOICES
from .phone_utils import to_e164


def validate_iraqi_mobile(value):
    """Field-level guard so admin forms and serializers both reject a bad
    number with a readable message instead of it reaching save()."""
    if value and not to_e164(value):
        raise ValidationError(
            'رقم هاتف عراقي غير صالح — يجب أن يبدأ بـ 075 أو 077 أو 078 أو 079'
        )


class User(AbstractUser):
    """Extended user model with trust scoring"""
    # The phone is the account's identity: it is required at signup, it is
    # how an account is recovered, and no two accounts may share one.
    #
    # NULL rather than '' for "no phone": MySQL allows any number of NULLs
    # in a unique index but only a single '', so blanks stored as '' would
    # make the second phone-less account (an admin, a fixture) fail to save.
    phone = models.CharField(
        max_length=20, blank=True, null=True, unique=True,
        validators=[validate_iraqi_mobile],
        help_text="E.164, normalised on save (+9647XXXXXXXXX)",
    )
    # Optional now that the phone carries identity, but still a lookup key
    # for password recovery, so one address cannot belong to two accounts.
    # AbstractUser declares it blank and non-unique; both are overridden.
    email = models.EmailField('email address', blank=True, null=True, unique=True)
    phone_verified = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    identity_verified = models.BooleanField(default=False)
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True)
    bio = models.TextField(max_length=500, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    # Updated from ChatConsumer on connect/disconnect — see
    # is_user_present_in_chat() for the live "online now" signal this backs.
    last_seen_at = models.DateTimeField(null=True, blank=True)
    trust_score = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(0), MaxValueValidator(5)]
    )
    total_sales = models.PositiveIntegerField(default=0)
    total_purchases = models.PositiveIntegerField(default=0)
    fcm_token = models.CharField(max_length=500, blank=True, null=True, help_text="Firebase Cloud Messaging token for push notifications")
    firebase_uid = models.CharField(max_length=128, blank=True, null=True, unique=True, db_index=True, help_text="Firebase Auth UID, set when the user signs in via Google/Facebook")
    # Explicit interests chosen during onboarding — optional/skippable, and
    # only a seed: UserCategoryAffinity (below) is what recommendations
    # actually rank on, and it keeps growing from behavior afterward.
    interests = models.ManyToManyField('Category', blank=True, related_name='interested_users')
    # Staff-only — suspension reasons, verification notes, etc. Never
    # exposed to the user themselves via the API.
    admin_notes = models.TextField(blank=True, help_text='ملاحظات داخلية للإدارة (أسباب الحظر، ملاحظات التحقق، ...)')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # One-time verification code, reused for email verification, phone
    # verification, and password reset (only one can be pending at a time —
    # requesting a new one for any purpose invalidates the previous one).
    VERIFICATION_PURPOSE_CHOICES = [
        ('email', 'Email Verification'),
        ('phone', 'Phone Verification'),
        ('password_reset', 'Password Reset'),
    ]
    verification_code = models.CharField(max_length=6, null=True, blank=True)
    verification_code_purpose = models.CharField(
        max_length=20, choices=VERIFICATION_PURPOSE_CHOICES, null=True, blank=True
    )
    verification_code_sent_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        """Store one canonical spelling of the phone, or nothing.

        A UNIQUE index compares strings, so without this '07701234567' and
        '+9647701234567' would be accepted as two accounts on one phone —
        the exact thing the constraint exists to prevent. Normalising here
        rather than only in the serializer means admin edits, management
        commands and fixtures obey the invariant too.

        Empty collapses to NULL for both fields (see the field comments).
        An unparseable non-empty phone raises instead of being silently
        dropped: validate_iraqi_mobile should already have rejected it, so
        reaching this means a caller bypassed validation, and losing the
        number quietly would cost the user their account recovery.
        """
        if self.phone:
            canonical = to_e164(self.phone)
            if not canonical:
                raise ValueError(f'Unparseable Iraqi mobile number: {self.phone!r}')
            self.phone = canonical
        else:
            self.phone = None

        self.email = (self.email or '').strip().lower() or None
        super().save(*args, **kwargs)

    def calculate_trust_score(self):
        """Calculate trust score based on ratings and history"""
        ratings = self.received_ratings.all()
        if ratings.exists():
            avg_rating = ratings.aggregate(models.Avg('score'))['score__avg']
            self.trust_score = Decimal(str(avg_rating))
            self.save()

    def generate_verification_code(self, purpose):
        """Generate and store a fresh 6-digit code for the given purpose."""
        import random
        code = f"{random.randint(0, 999999):06d}"
        self.verification_code = code
        self.verification_code_purpose = purpose
        self.verification_code_sent_at = timezone.now()
        self.save(update_fields=[
            'verification_code', 'verification_code_purpose', 'verification_code_sent_at'
        ])
        return code

    def check_verification_code(self, purpose, code, validity_minutes=15):
        """Check a submitted code against the stored one for the given purpose."""
        if not self.verification_code or self.verification_code_purpose != purpose:
            return False
        if not self.verification_code_sent_at:
            return False
        if timezone.now() - self.verification_code_sent_at > timedelta(minutes=validity_minutes):
            return False
        return self.verification_code == code

    def clear_verification_code(self):
        self.verification_code = None
        self.verification_code_purpose = None
        self.verification_code_sent_at = None
        self.save(update_fields=[
            'verification_code', 'verification_code_purpose', 'verification_code_sent_at'
        ])

    class Meta:
        verbose_name = 'مستخدم'
        verbose_name_plural = 'المستخدمون'


class Location(models.Model):
    """Location model for items and users"""
    LOCATION_TYPE_CHOICES = [
        ('user_address', 'User Address'),
        ('warehouse', 'Warehouse'),
        ('store', 'Store'),
        ('other', 'Other'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='locations', null=True, blank=True)
    location_type = models.CharField(max_length=20, choices=LOCATION_TYPE_CHOICES, default='user_address')
    name = models.CharField(max_length=200, help_text="Location name (e.g., 'My Home', 'Main Warehouse')")

    # Address fields
    address_line1 = models.CharField(max_length=255)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    # المحافظة — constrained to Iraq's fixed 18 governorates so search by
    # governorate is a reliable exact match instead of free-text guessing.
    state = models.CharField(max_length=100, choices=IRAQ_GOVERNORATE_CHOICES)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100)

    # GPS coordinates (optional)
    latitude = models.DecimalField(max_digits=11, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=11, decimal_places=6, null=True, blank=True)

    # Contact info — required so a courier always has someone to reach
    contact_person = models.CharField(max_length=200)
    contact_phone = models.CharField(max_length=20)

    # Additional info
    notes = models.TextField(blank=True, help_text="Special delivery instructions")
    is_primary = models.BooleanField(default=False, help_text="Primary location for this user")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_primary', '-created_at']
        verbose_name = 'موقع'
        verbose_name_plural = 'المواقع والعناوين'

    def __str__(self):
        return f"{self.name} - {self.city}, {self.country}"

    def get_full_address(self):
        """Return formatted full address"""
        parts = [self.address_line1]
        if self.address_line2:
            parts.append(self.address_line2)
        parts.append(f"{self.city}, {self.state} {self.postal_code}")
        parts.append(self.country)
        return ", ".join(parts)


class UserStore(models.Model):
    """Each user's store/profile"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='store')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    logo = models.ImageField(upload_to='store_logos/', null=True, blank=True)
    cover_image = models.ImageField(upload_to='store_covers/', null=True, blank=True)
    is_active = models.BooleanField(default=True)
    phone_number = models.CharField(
        max_length=20, blank=True,
        help_text="رقم هاتف المتجر للتواصل"
    )
    whatsapp_number = models.CharField(
        max_length=20, blank=True,
        help_text="رقم واتساب المتجر (مع رمز الدولة، بدون + أو أصفار بادئة)"
    )
    rating = models.DecimalField(
        max_digits=3, decimal_places=2, default=Decimal('0.00'),
        validators=[MinValueValidator(0), MaxValueValidator(5)]
    )

    # NEW: Default location for this store
    default_location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='default_for_stores',
        help_text="Default location for items in this store"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'متجر'
        verbose_name_plural = 'المتاجر'

    def __str__(self):
        return f"{self.user.username}'s Store - {self.name}"

    # Performance level (برونزي/فضّي/ذهبي/ماسي). Derived — recomputed by
    # tasks.recompute_store_levels from confirmed sales, rating and
    # followers (rules in services/store_badges.py); never edited by hand.
    LEVEL_NONE, LEVEL_BRONZE, LEVEL_SILVER, LEVEL_GOLD, LEVEL_DIAMOND = '', 'bronze', 'silver', 'gold', 'diamond'
    LEVEL_CHOICES = [
        (LEVEL_NONE, 'بلا مستوى'), (LEVEL_BRONZE, 'برونزي'), (LEVEL_SILVER, 'فضّي'),
        (LEVEL_GOLD, 'ذهبي'), (LEVEL_DIAMOND, 'ماسي'),
    ]
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='', blank=True, db_index=True)
    level_updated_at = models.DateTimeField(null=True, blank=True)

    def update_rating(self):
        """Recalculate average rating from all store reviews"""
        avg = self.store_reviews.aggregate(models.Avg('score'))['score__avg']
        self.rating = Decimal(str(avg)) if avg else Decimal('0.00')
        self.save(update_fields=['rating'])

    def badge_kinds(self):
        """Every badge this store currently shows, granted and derived alike,
        in display order. What the app renders next to the store name."""
        from .services.store_badges import badges_for_store
        return badges_for_store(self)


class StoreBadge(models.Model):
    """A badge the platform *gave* a store — as opposed to the performance
    level, which the store earns and loses on its own (UserStore.level).

    founder / pioneer / certified are admin-only and capped (the first 50
    stores, the next 200, course graduates). `verified` is granted
    automatically when the owner's identity and phone are verified, but is
    still a row so support can see when and revoke it. Revoking never
    deletes: the row keeps `revoked_at` so a re-grant is visibly a re-grant."""
    KIND_FOUNDER, KIND_PIONEER, KIND_CERTIFIED, KIND_VERIFIED = 'founder', 'pioneer', 'certified', 'verified'
    KIND_CHOICES = [
        (KIND_FOUNDER, 'مؤسِّس'), (KIND_PIONEER, 'رائد'),
        (KIND_CERTIFIED, 'بائع معتمد'), (KIND_VERIFIED, 'موثَّق'),
    ]
    ADMIN_ONLY = (KIND_FOUNDER, KIND_PIONEER, KIND_CERTIFIED)
    # Founders are the first 50 stores, pioneers the next 200: caps, not
    # first-come — the admin chooses who, the cap just cannot be exceeded.
    CAPS = {KIND_FOUNDER: 50, KIND_PIONEER: 200}
    # Lifetime platform-commission multiplier the badge promises. There is
    # no platform commission charged yet; this is the single place the
    # promise is recorded so the fee code, when it comes, reads it here.
    COMMISSION_MULTIPLIER = {KIND_FOUNDER: Decimal('0'), KIND_PIONEER: Decimal('0.5')}

    store = models.ForeignKey(UserStore, on_delete=models.CASCADE, related_name='badges')
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    granted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='badges_granted',
        help_text='فارغ = مُنح تلقائياً')
    note = models.CharField(max_length=200, blank=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        verbose_name = 'شارة متجر'
        verbose_name_plural = 'شارات المتاجر'
        ordering = ['-granted_at']
        constraints = [
            # One live badge of a kind per store; history rows (revoked) may repeat.
            models.UniqueConstraint(
                fields=['store', 'kind'], condition=models.Q(revoked_at__isnull=True),
                name='one_active_badge_per_kind'),
        ]

    def __str__(self):
        return '%s — %s' % (self.store.name, self.get_kind_display())

    @property
    def is_active(self):
        return self.revoked_at is None


class Category(models.Model):
    """Item categories"""
    name = models.CharField(max_length=100)
    name_ar = models.CharField(max_length=100, blank=True)
    slug = models.SlugField(unique=True)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='children')
    icon = models.CharField(
        max_length=255, blank=True,
        help_text='رمز Material Icon الرقمي (مثال: 57688) أو رابط صورة مباشر — يُتجاهل إذا رُفعت صورة أدناه.',
    )
    image = models.ImageField(
        upload_to='categories/', blank=True, null=True,
        help_text='صورة/أيقونة مرفوعة للتصنيف — لها الأولوية على حقل "icon" أعلاه.',
    )
    order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    # Platform commission taken from the SELLER on every released sale in
    # this category, as a percentage of the item price. Blank = inherit
    # from the parent category, then from settings.PLATFORM_COMMISSION_PERCENT.
    # Read through effective_commission_percent(); never this field directly.
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='نسبة عمولة المنصّة من البائع (%)؛ فارغ = ترث من الفئة الأم أو الافتراضي',
    )

    class Meta:
        verbose_name = 'تصنيف'
        verbose_name_plural = 'التصنيفات'
        ordering = ['order']

    def __str__(self):
        return self.name

    def effective_commission_percent(self):
        """This category's rate, or the nearest ancestor's, or the platform
        default. Walks up at most the tree's depth (a few levels)."""
        from django.conf import settings
        node, seen = self, 0
        while node is not None and seen < 10:
            if node.commission_percent is not None:
                return node.commission_percent
            node, seen = node.parent, seen + 1
        return Decimal(str(getattr(settings, 'PLATFORM_COMMISSION_PERCENT', '0')))


class UserCategoryAffinity(models.Model):
    """How strongly a user is interested in a category — seeded from the
    onboarding interest picker, reinforced by behavior (purchases, store
    follows). A plain incrementing counter rather than an aggregate over raw
    event logs, so ranking recommendations is a cheap indexed read instead of
    a GROUP BY over history on every request."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='category_affinities')
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='user_affinities')
    score = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'category')
        indexes = [models.Index(fields=['user', '-score'])]
        verbose_name = 'اهتمام المستخدم بالتصنيف'
        verbose_name_plural = 'اهتمامات المستخدمين بالتصنيفات'

    def __str__(self):
        return f"{self.user.username} → {self.category.name} ({self.score})"


def record_interest(user, category, weight=1):
    """Bump a user's affinity score for a category. Safe to call from hot
    paths (purchase completion, store follow) — a single get_or_create plus
    an F()-expression UPDATE, no read-modify-write race and no heavy query."""
    if user is None or category is None:
        return
    affinity, created = UserCategoryAffinity.objects.get_or_create(
        user=user, category=category, defaults={'score': weight}
    )
    if not created:
        UserCategoryAffinity.objects.filter(pk=affinity.pk).update(score=models.F('score') + weight)


# Coarse package-size tiers, used only to decide how many reservations from
# one seller can ride in a single delivery trip (see
# delivery_utils.pack_into_delivery_groups) — deliberately not exact
# dimensions/weight, since that's more precision than a casual seller will
# reliably fill in.
PACKAGE_SIZE_CHOICES = [
    ('small', 'صغير'),
    ('medium', 'متوسط'),
    ('large', 'كبير'),
    ('bulky', 'ضخم'),
]
PACKAGE_SIZE_UNITS = {'small': 1, 'medium': 2, 'large': 4, 'bulky': 8}


class Item(models.Model):
    """Core item model"""
    CONDITION_CHOICES = [
        ('new', 'New'),
        ('like_new', 'Like New'),
        ('good', 'Good'),
        ('fair', 'Fair'),
        ('poor', 'Poor'),
    ]

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('reserved', 'Reserved'),
        ('sold', 'Sold'),
        ('transferred', 'Transferred'),
        ('archived', 'Archived'),
    ]

    # Ownership
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_items')
    current_holder = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='held_items',
        help_text="User currently holding/storing the item"
    )

    # Basic Info
    title = models.CharField(max_length=200)
    name_ar = models.CharField(max_length=200, blank=True)
    description = models.TextField()
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES)
    # Kept as the denormalised TOTAL across variants. Stock actually lives on
    # ItemVariant rows; every read site that only wants "how many are left"
    # keeps working unchanged, while every write goes through
    # ItemVariant.take_stock / return_stock, which recompute this.
    quantity = models.PositiveIntegerField(default=1, help_text="Number of items available")

    # The axes a buyer picks along, e.g.
    #   [{"name": "اللون", "values": ["أحمر", "أزرق"]},
    #    {"name": "المقاس", "values": ["M", "L"]}]
    # Empty for a plain item. JSON rather than tables because these are only
    # ever rendered inside one product page — nothing queries "every red item
    # in the marketplace" — and a nested table trio would buy that query at
    # the price of three joins on every product read.
    options = models.JSONField(default=list, blank=True)
    package_size = models.CharField(
        max_length=10, choices=PACKAGE_SIZE_CHOICES, default='medium',
        help_text='الحجم التقريبي للطرد — يُستخدم لتحديد عدد المنتجات القابلة لدمجها بشحنة توصيل واحدة'
    )
    warranty_days = models.PositiveIntegerField(
        default=7,
        validators=[MinValueValidator(1)],
        help_text='مدة الضمان بالأيام قبل تحرير الدفعة المحجوزة تلقائياً للبائع (ما لم يوافق المشتري أو يُفتح نزاع). الحد الأدنى يوم واحد — انظر Reservation.can_release_payment.'
    )

    # Home screen metrics
    sold_count = models.IntegerField(default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('0.00'),
                                 validators=[MinValueValidator(0), MaxValueValidator(5)])
    is_daily_deal = models.BooleanField(default=False)

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')

    # NEW: Location tracking
    current_location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='items_at_location',
        help_text="Current physical location of the item"
    )

    # Staff-only — why a moderator archived/hid this listing.
    admin_notes = models.TextField(blank=True, help_text='ملاحظات إدارية (سبب الإخفاء/الأرشفة، ...)')

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'منتج'
        verbose_name_plural = 'المنتجات'
        indexes = [
            models.Index(fields=['category', 'status']),
            models.Index(fields=['status', 'condition']),
        ]

    @property
    def default_variant(self):
        """The variant a plain item carries. Also the fallback for any legacy
        reservation created before variants existed (variant FK is null)."""
        return self.variants.filter(is_active=True).order_by('id').first()

    def sync_stock_from_variants(self):
        """Recomputes the denormalised total and the derived status.

        Status follows stock the same way the old per-item code did: sold out
        when nothing is left, otherwise active — except a draft stays a draft,
        the seller hasn't published it yet regardless of what's in stock."""
        from django.db.models import Sum
        total = self.variants.filter(is_active=True).aggregate(
            t=Sum('quantity'))['t'] or 0
        self.quantity = total
        if self.status != 'draft':
            self.status = 'sold' if total == 0 else 'active'
        self.save(update_fields=['quantity', 'status'])

    def __str__(self):
        return f"{self.title} - {self.owner.username}"

    @property
    def package_units(self):
        return PACKAGE_SIZE_UNITS.get(self.package_size, PACKAGE_SIZE_UNITS['medium'])

    def update_rating(self):
        """Recalculate average rating from all product reviews"""
        avg = self.product_reviews.aggregate(models.Avg('score'))['score__avg']
        self.rating = Decimal(str(avg)) if avg else Decimal('0.00')
        self.save(update_fields=['rating'])

    def update_location(self, new_location):
        """Update item location and create history entry"""
        if self.current_location != new_location:
            # Create location history
            ItemLocationHistory.objects.create(
                item=self,
                location=new_location,
                moved_by=self.current_holder or self.owner,
                moved_at=timezone.now()
            )
            self.current_location = new_location
            self.save(update_fields=['current_location'])


class ItemLocationHistory(models.Model):
    """Track item location changes"""
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='location_history')
    location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True)
    moved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    moved_at = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-moved_at']
        verbose_name = 'سجل تنقّل منتج'
        verbose_name_plural = 'سجل تنقّل المنتجات'

    def __str__(self):
        return f"{self.item.title} moved to {self.location} on {self.moved_at}"


class ItemVariant(models.Model):
    """One sellable combination of an item's options — what a buyer actually
    reserves, what stock is counted on, and what a wholesale transfer moves.

    Every item has at least one. A plain item with no options carries a single
    variant with empty `attributes` holding all of its stock; the data
    migration creates it for every pre-existing row. That invariant is what
    lets reservations, transfers and delivery all speak to a variant without
    a "but what if the item has none" branch anywhere.

    `price` is an override: null means "the item's listing price". `image` is
    likewise optional and falls back to the item's gallery. A variant is
    deactivated rather than deleted once anything references it, so a
    reservation for "red / L" still knows what it was for after the seller
    drops red from the range.
    """
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='variants')
    # {"اللون": "أحمر", "المقاس": "L"} — keys are the names in Item.options.
    attributes = models.JSONField(default=dict, blank=True)
    sku = models.CharField(max_length=64, blank=True)
    quantity = models.PositiveIntegerField(default=0)
    price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='سعر خاص بهذا المتغيّر؛ فارغ = سعر المنتج الأساسي'
    )
    # What the merchant paid per unit. Optional and private to the seller:
    # the dashboard uses it for gross profit, the buyer never sees it.
    cost_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='كلفة الوحدة على التاجر (اختياري، لا تظهر للمشتري)'
    )
    image = models.ForeignKey(
        'ItemImage', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='variants'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = 'متغيّر منتج'
        verbose_name_plural = 'متغيّرات المنتجات'

    def __str__(self):
        return "%s [%s]" % (self.item_id, self.label or 'default')

    @property
    def label(self):
        """'أحمر / L' — for receipts, admin and notifications.

        Ordered by the item's option declaration, not the JSON's key order:
        MySQL normalises object keys (shorter first), which would turn
        اللون / المقاس into المقاس / اللون on the way back out."""
        attrs = self.attributes or {}
        names = [str(o.get('name')) for o in (self.item.options or [])]
        ordered = [attrs[n] for n in names if n in attrs]
        rest = [v for k, v in attrs.items() if k not in names]
        return " / ".join(str(v) for v in ordered + rest)

    @property
    def effective_price(self):
        if self.price is not None:
            return self.price
        listing = getattr(self.item, 'fixed_listing', None)
        return listing.price if listing else None

    # -- the only two ways stock changes ---------------------------------
    def take_stock(self, qty):
        """Removes [qty] units. Raises ValueError when there aren't enough,
        so a caller can never overdraw by racing another buyer — call this on
        a `select_for_update()`-locked row inside the caller's transaction."""
        if qty > self.quantity:
            raise ValueError("Insufficient stock. Only %d available" % self.quantity)
        self.quantity -= qty
        self.save(update_fields=['quantity'])
        self.item.sync_stock_from_variants()

    def return_stock(self, qty):
        self.quantity += qty
        self.save(update_fields=['quantity'])
        self.item.sync_stock_from_variants()


class ItemImage(models.Model):
    """Item images"""
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='item_images/')
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']
        verbose_name = 'صورة منتج'
        verbose_name_plural = 'صور المنتجات'


class Favorite(models.Model):
    """A buyer marking an item as a favorite/wishlist entry — mirrors ReelLike's pattern."""
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='favorited_by')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='favorites')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['item', 'user']
        ordering = ['-created_at']
        verbose_name = 'مفضلة'
        verbose_name_plural = 'المفضلات'

    def __str__(self):
        return f"{self.user.username} favorited item #{self.item_id}"


class FixedPriceListing(models.Model):
    """Fixed price sale listing"""
    item = models.OneToOneField(Item, on_delete=models.CASCADE, related_name='fixed_listing')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    original_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Pre-discount reference price, used to show a discount badge"
    )
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2)
    deposit_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'قائمة سعر ثابت'
        verbose_name_plural = 'قوائم السعر الثابت'

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.original_price is not None and self.price is not None and self.price >= self.original_price:
            raise ValidationError({'price': 'Price must be lower than the original price when a discount is set.'})

    def save(self, *args, **kwargs):
        self.deposit_amount = (self.price * self.deposit_percentage) / 100
        super().save(*args, **kwargs)

    @property
    def discount_percentage(self):
        if self.original_price and self.original_price > 0 and self.price is not None:
            return round((1 - self.price / self.original_price) * 100)
        return 0


class AuctionListing(models.Model):
    """Auction sale listing"""
    STATUS_CHOICES = [
        ('upcoming', 'Upcoming'),
        ('active', 'Active'),
        ('ended', 'Ended'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    item = models.OneToOneField(Item, on_delete=models.CASCADE, related_name='auction_listing')
    starting_price = models.DecimalField(max_digits=10, decimal_places=2)
    current_price = models.DecimalField(max_digits=10, decimal_places=2)
    minimum_increment = models.DecimalField(max_digits=10, decimal_places=2)
    bid_deposit = models.DecimalField(max_digits=10, decimal_places=2)

    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='upcoming')
    winner = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='won_auctions'
    )

    # Set if this auction was started as a quick 2-minute live auction during
    # a broadcast (see LiveSessionViewSet.quick_auction). Bidding/settlement
    # are otherwise completely unchanged — this only adds a live broadcast
    # channel for bid updates and the final result (see place_bid's
    # _broadcast_auction_update and tasks.finalize_live_auction).
    live_session = models.ForeignKey(
        'LiveSession', on_delete=models.SET_NULL, null=True, blank=True, related_name='auctions'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'مزاد'
        verbose_name_plural = 'المزادات'

    def is_active(self):
        now = timezone.now()
        return self.start_time <= now <= self.end_time and self.status == 'active'


class Bid(models.Model):
    """Auction bids"""
    auction = models.ForeignKey(AuctionListing, on_delete=models.CASCADE, related_name='bids')
    bidder = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bids')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    deposit_paid = models.BooleanField(default=False)
    deposit_refunded = models.BooleanField(default=False)
    is_winning = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-amount', '-created_at']
        verbose_name = 'مزايدة'
        verbose_name_plural = 'المزايدات'


class ItemTransfer(models.Model):
    """
    Wholesale Ownership Transfer:
    - to_user (reseller) requests a quantity of from_user's (owner) item
    - On approval, that quantity is deducted from the owner's stock and a new,
      fully independent Item is created for the reseller (their own to price,
      sell, or transfer freely)
    - The reseller pays the owner a wholesale price for the quantity, taken
      from their wallet at approval time
    - The stock physically stays in the owner's warehouse, so the owner may
      still charge the reseller storage fees
    """
    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('active', 'Active'),
        ('rejected', 'Rejected'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('expired', 'Expired'),
    ]

    STORAGE_FEE_TYPE_CHOICES = [
        ('none', 'No Storage Fee'),
        ('flat_rate', 'Flat Rate per Period'),
        ('percentage', 'Percentage of Resale Price'),
    ]

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='transfers')
    from_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='transfers_as_owner',
        help_text="Original owner — item stays physically with them"
    )
    to_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='transfers_as_reseller',
        help_text="Reseller — lists the item in their online store"
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Agreement terms
    request_message = models.TextField(blank=True, help_text="Message from reseller to owner")
    rejection_reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    # Duration of the hosting agreement (null = open-ended)
    duration_days = models.PositiveIntegerField(null=True, blank=True)
    agreement_start_date = models.DateTimeField(null=True, blank=True)
    agreement_end_date = models.DateTimeField(null=True, blank=True)

    # --- WHOLESALE PURCHASE: quantity requested and the price the owner sets for it ---
    quantity = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)],
        help_text="Quantity of the item being transferred to the reseller's ownership"
    )
    # Which combination is being bought wholesale. Same PROTECT/nullable
    # reasoning as Reservation.variant.
    variant = models.ForeignKey(
        'ItemVariant', on_delete=models.PROTECT, null=True, blank=True,
        related_name='transfers'
    )
    wholesale_price_per_unit = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Snapshot of the item's per-unit price at approval time"
    )
    resulting_item = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='acquired_via_transfer',
        help_text="The reseller's new, fully-owned item created on approval"
    )

    # --- STORAGE FEES: reseller pays owner periodically to keep stock in their warehouse ---
    storage_fee_type = models.CharField(
        max_length=20, choices=STORAGE_FEE_TYPE_CHOICES, default='none'
    )
    storage_fee_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    storage_fee_period_days = models.PositiveIntegerField(default=30)
    total_storage_fees_paid = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00')
    )
    next_storage_fee_due = models.DateTimeField(null=True, blank=True)

    # Timestamps
    initiated_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-initiated_at']
        verbose_name = 'اتفاقية إعادة بيع'
        verbose_name_plural = 'اتفاقيات إعادة البيع'

    def __str__(self):
        return f"Resale agreement: {self.item.title} | {self.from_user.username} → {self.to_user.username}"

    def is_expired(self):
        if self.status == 'active' and self.agreement_end_date:
            return timezone.now() > self.agreement_end_date
        return False

    def days_remaining(self):
        if self.status == 'active' and self.agreement_end_date:
            return max(0, (self.agreement_end_date - timezone.now()).days)
        return None

    def term_has_ended(self):
        """True if there's no fixed hosting term, or the agreed term has already passed."""
        if not self.agreement_end_date:
            return True
        return timezone.now() >= self.agreement_end_date

    @property
    def total_wholesale_price(self):
        if self.wholesale_price_per_unit is None:
            return None
        return self.quantity * self.wholesale_price_per_unit

    def activate(self):
        """
        Owner approves → deducts the requested quantity from their item's stock,
        charges the reseller's wallet at the item's listed per-unit price, and
        creates a new, fully independent Item owned by the reseller.
        """
        from .wallet_utils import debit_balance, credit_earnings

        if self.status != 'pending':
            raise ValueError("Only pending agreements can be activated")

        item = self.item
        fixed_listing = getattr(item, 'fixed_listing', None)
        if not fixed_listing or not fixed_listing.price or fixed_listing.price <= 0:
            raise ValueError("Item has no fixed price listing")

        # Stock comes off one specific combination. A transfer that named no
        # variant (legacy, or a plain item) resolves to the default one.
        variant = self.variant or item.default_variant
        if variant is None:
            raise ValueError("Item has no sellable variant")
        variant = ItemVariant.objects.select_for_update().get(pk=variant.pk)
        if self.quantity > variant.quantity:
            raise ValueError(f"Insufficient stock. Only {variant.quantity} available")

        # A variant can carry its own price; wholesale is struck on whatever
        # the buyer would actually pay for that combination.
        wholesale_price_per_unit = variant.price if variant.price is not None else fixed_listing.price
        total_price = wholesale_price_per_unit * self.quantity

        variant.take_stock(self.quantity)

        debit_balance(self.to_user, total_price, 'purchase', item=item, transfer=self)
        credit_earnings(self.from_user, total_price, 'payout', item=item, transfer=self)

        new_item = Item.objects.create(
            owner=self.to_user,
            current_holder=self.to_user,
            title=item.title,
            name_ar=item.name_ar,
            description=item.description,
            category=item.category,
            condition=item.condition,
            quantity=self.quantity,
            current_location=item.current_location,
            status='draft',
        )
        for img in item.images.all():
            ItemImage.objects.create(item=new_item, image=img.image, order=img.order)
        # The reseller's copy is a single-combination item: they bought "red /
        # L", so that is all their listing offers. The post_save signal already
        # created its one variant holding `quantity`; label it with the
        # attributes they were sold rather than adding a second row, which
        # would double the stock.
        new_variant = new_item.default_variant
        new_variant.attributes = dict(variant.attributes or {})
        new_variant.sku = variant.sku
        new_variant.save(update_fields=['attributes', 'sku'])

        now = timezone.now()
        self.wholesale_price_per_unit = wholesale_price_per_unit
        self.resulting_item = new_item
        self.status = 'active'
        self.approved_at = now
        self.agreement_start_date = now
        if self.duration_days:
            self.agreement_end_date = now + timedelta(days=self.duration_days)
        if self.storage_fee_type != 'none':
            self.next_storage_fee_due = now + timedelta(days=self.storage_fee_period_days)
        self.save(update_fields=[
            'wholesale_price_per_unit', 'resulting_item', 'status', 'approved_at',
            'agreement_start_date', 'agreement_end_date', 'next_storage_fee_due',
        ])

    def calculate_due_storage_fee(self):
        """How much is owed for the current storage period."""
        if self.storage_fee_type == 'none':
            return Decimal('0.00')
        if self.storage_fee_type == 'flat_rate':
            return self.storage_fee_amount or Decimal('0.00')
        if self.storage_fee_type == 'percentage' and self.total_wholesale_price:
            return (self.total_wholesale_price * (self.storage_fee_amount or Decimal('0.00'))) / 100
        return Decimal('0.00')

    def is_storage_fee_due(self):
        if not self.next_storage_fee_due or self.storage_fee_type == 'none':
            return False
        return timezone.now() >= self.next_storage_fee_due


class StorageFeePayment(models.Model):
    """Track storage fee payments for item transfers"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('overdue', 'Overdue'),
        ('waived', 'Waived'),
    ]

    transfer = models.ForeignKey(ItemTransfer, on_delete=models.CASCADE, related_name='storage_fee_payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    due_date = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    paid_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'دفعة رسوم تخزين'
        verbose_name_plural = 'رسوم التخزين'

    def __str__(self):
        return f"Storage fee ${self.amount} for {self.transfer} - {self.status}"

    def is_overdue(self):
        """Check if payment is overdue"""
        return self.status == 'pending' and timezone.now() > self.due_date


class Reservation(models.Model):
    """Item reservation with deposit - ENHANCED to support transferred items

    Status lifecycle:
      pending   -> buyer has reserved the item but not yet paid; expires at `expires_at`.
      completed -> buyer finished the in-app payment step (deposit-only or
                   full — see `is_deposit_only`). Money is held; item stock is
                   taken now. This is the window during which a dispute can
                   be filed (see ReservationViewSet.create_dispute).
      confirmed -> the sale is fully settled: the full price has been
                   collected (in-app, or in cash at delivery for
                   deposit-only orders) AND the buyer confirmed receipt
                   (approve_and_release_payment) or the item's warranty
                   period passed with no open dispute
                   (tasks.release_payments_after_week). The warranty clock
                   is anchored to when the buyer actually received the item
                   (DeliveryOrder.delivered_at — see `received_at` below),
                   never to `completed_at`/payment time, so a reservation
                   the seller hasn't shipped yet can never age into
                   'confirmed' on its own. Terminal success state.
      expired   -> 'pending' reservation whose hold ran out unpaid.
      cancelled -> buyer or seller cancelled (seller may cancel even a paid
                   'completed'/'confirmed' reservation; the buyer is refunded).
      disputed  -> a dispute was filed while 'completed'; resolved by an
                   admin back to 'confirmed' (pay_seller/no_action) or
                   'refunded' (refund_buyer/partial_refund).
      refunded  -> buyer was refunded (in full or in part) via dispute resolution.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('completed', 'Completed'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
        ('disputed', 'Disputed'),
        ('refunded', 'Refunded'),
    ]

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='reservations')
    # Which combination was reserved. PROTECT rather than SET_NULL: a variant
    # with reservations against it is deactivated, never deleted, so the
    # receipt for "red / L" keeps saying red / L. Nullable only for rows that
    # predate variants — see `stock_variant`.
    variant = models.ForeignKey(
        'ItemVariant', on_delete=models.PROTECT, null=True, blank=True,
        related_name='reservations'
    )
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reservations')

    quantity = models.PositiveIntegerField(default=1)
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    # Charged to the buyer in full at complete() regardless of payment_type —
    # computed from the active delivery company's intra/inter-governorate
    # rate (0 if no active company at that time). See ReservationViewSet.complete.
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    # Snapshot of item.warranty_days taken when the reservation completes
    # (see wallet_utils.complete_reservation) — a later edit to the item's
    # warranty by the seller must not retroactively change the terms of an
    # already in-flight order. See can_release_payment below.
    warranty_days = models.PositiveIntegerField(default=7)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    reserved_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    # Set when the buyer finishes their in-app payment step (deposit-only or
    # full) — i.e. when status first becomes 'completed'. 'confirmed' is a
    # later, separate milestone (see STATUS_CHOICES below) so this timestamp
    # does not move again after that.
    completed_at = models.DateTimeField(null=True, blank=True)
    # True if the buyer paid only the deposit at completion (remainder is
    # collected in cash at delivery, outside the app). Kept separate from
    # `status` because status moves on through 'confirmed'/'disputed'/
    # 'cancelled'/'refunded', at which point we still need to know which
    # amount was actually charged in-app.
    is_deposit_only = models.BooleanField(default=False)

    # Approval fields
    buyer_approved = models.BooleanField(default=False)
    buyer_approved_at = models.DateTimeField(null=True, blank=True)
    seller_cancellation_approved = models.BooleanField(default=False)
    payment_released_to_seller = models.BooleanField(default=False)
    payment_released_at = models.DateTimeField(null=True, blank=True)

    # Representative fields
    representative = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='representative_sales',
        help_text='Representative who facilitated this sale'
    )
    representative_assignment = models.ForeignKey(
        'Representative',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reservations',
        help_text='The representative assignment used for this sale'
    )
    commission_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Commission amount for representative'
    )
    commission_paid = models.BooleanField(default=False)
    commission_paid_at = models.DateTimeField(null=True, blank=True)

    # Platform commission, frozen at creation like the price: the category
    # rate (after the store's badge multiplier — founders pay 0, pioneers
    # half) applied to total_price. Taken from the SELLER's share when the
    # payment is released — never before, the money is in escrow until then.
    platform_commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'))
    platform_commission_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'))

    # NEW: Transfer-related fields
    is_transfer_resale = models.BooleanField(
        default=False,
        help_text="Is this a resale during an item transfer?"
    )
    related_transfer = models.ForeignKey(
        ItemTransfer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transfer_reservations',
        help_text="The transfer this reservation is part of (if applicable)"
    )
    original_owner_commission = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Commission for original owner if sold during transfer"
    )

    # NEW: Delivery location
    delivery_location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deliveries',
        help_text="Where the item should be delivered"
    )

    # Coupon applied at reservation time (كوبون المتجر). OneToOne so a
    # collected coupon can be spent on at most one reservation (single use).
    applied_coupon = models.OneToOneField(
        'CouponCollection',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='used_in_reservation',
        help_text='The collected store coupon redeemed for this reservation'
    )
    coupon_discount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text='Amount discounted from total_price by the applied coupon'
    )

    # Accepted chat offer redeemed at reservation time (عرض مقبول بالشات).
    # OneToOne so an accepted offer can be spent on at most one reservation.
    applied_offer = models.OneToOneField(
        'Message',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='redeemed_in_reservation',
        help_text='The accepted chat offer message this reservation was priced from'
    )

    # Set if this reservation was made during a live shopping broadcast
    live_session = models.ForeignKey(
        'LiveSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reservations',
        help_text='The live broadcast this reservation was made during, if any'
    )
    live_session_item = models.ForeignKey(
        'LiveSessionItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reservations',
        help_text='The live-session queue entry (price/deposit snapshot) this was reserved from'
    )
    auction = models.OneToOneField(
        'AuctionListing',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='winner_reservation',
        help_text=(
            'The auction this reservation settles, if any. deposit_amount here is the bid '
            'deposit the winner already paid while bidding — complete() must not re-charge it.'
        )
    )
    proof_screenshot = models.ImageField(
        upload_to='live_reservation_proofs/',
        null=True,
        blank=True,
        help_text='Client-captured frame of the live video at the moment of reservation'
    )

    def is_expired(self):
        return timezone.now() > self.expires_at and self.status == 'pending'

    @property
    def amount_charged(self):
        """
        How much is releasable in total so far. delivery_fee follows
        whichever timing the buyer chose for the item itself (see
        wallet_utils.complete_reservation): for a full payment it's charged
        in-app immediately, so it's part of the total right away; for a
        deposit-only sale it's deferred to COD, bundled into the delivery
        order's cash_to_collect alongside the item's remainder, so it only
        counts in once the company has confirmed collecting that cash.
        Based on completed_at/is_deposit_only rather than the current
        status, since status can move on to 'disputed'/'cancelled'/
        'refunded' while this needs to keep reflecting what was really
        charged/collected. Split between the seller and the delivery
        company by release_reservation_payment.
        """
        if not self.completed_at:
            return Decimal('0.00')
        if not self.is_deposit_only:
            return self.total_price + self.delivery_fee
        charged = self.deposit_amount
        outbound = self.delivery_orders.filter(direction='outbound', cash_collected=True).first()
        if outbound:
            # cash_due_to_platform, not cash_to_collect: the company kept its
            # own share of the fee out of the cash, so that part never
            # reaches the platform and cannot be released to anyone. The
            # platform's cut of the fee DID arrive (inside the settlement)
            # and is paid out in release_reservation_payment.
            charged += outbound.cash_due_to_platform
        return charged

    def can_buyer_cancel(self):
        """Check if buyer can cancel (within 2 hours or seller approved)"""
        time_since_reservation = timezone.now() - self.reserved_at
        within_two_hours = time_since_reservation.total_seconds() <= 7200  # 2 hours
        return within_two_hours or self.seller_cancellation_approved

    @property
    def received_at(self):
        """
        When the buyer actually got the item in hand, if that has happened
        yet — the anchor for the warranty clock in can_release_payment().
        Deliberately NOT completed_at/payment time: a reservation can sit
        'completed' for a long time before the seller ships it (send_to_delivery
        is a separate, later action), and payment landing in the seller's
        account has to wait for delivery either way.

        Sourced from the outbound DeliveryOrder reaching the buyer's door
        (status 'delivered' or the buyer-confirmed 'confirmed'), not from
        'assigned'/'picked_up' — those mean the company has the item, not
        the buyer. None if no outbound leg has been delivered yet (including
        when the seller hasn't sent it to a delivery company at all), in
        which case can_release_payment() can only succeed via buyer_approved.
        """
        outbound = self.delivery_orders.filter(
            direction='outbound',
            status__in=['delivered', 'confirmed'],
            delivered_at__isnull=False,
        ).order_by('-delivered_at').first()
        return outbound.delivered_at if outbound else None

    def can_release_payment(self):
        """Check if payment can be released to seller"""
        if self.payment_released_to_seller or self.status not in ('completed', 'confirmed'):
            return False

        # Payment released if buyer approved OR the item's warranty period
        # passed without dispute, counted from when the buyer received the
        # item (see `received_at`) — not from payment time.
        if self.buyer_approved:
            return True

        received_at = self.received_at
        if received_at:
            warranty_passed = timezone.now() - received_at >= timedelta(days=self.warranty_days)
            has_open_dispute = Dispute.objects.filter(
                reservation=self,
                status__in=['open', 'investigating']
            ).exists()
            return warranty_passed and not has_open_dispute

        return False

    class Meta:
        verbose_name = 'حجز'
        verbose_name_plural = 'الحجوزات'

    @property
    def stock_variant(self):
        """The variant this reservation's stock lives on. Legacy rows carry
        no variant and resolve to the item's default one — the same row the
        data migration moved their stock into."""
        return self.variant or self.item.default_variant

    def __str__(self):
        return f"Reservation #{self.id} - {self.item.title} by {self.buyer.username}"


class Dispute(models.Model):
    """Dispute resolution system"""
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('investigating', 'Investigating'),
        ('resolved', 'Resolved'),
        ('closed', 'Closed'),
    ]

    RESOLUTION_CHOICES = [
        ('refund_buyer', 'Refund to Buyer'),
        ('pay_seller', 'Pay Seller'),
        ('partial_refund', 'Partial Refund'),
        ('no_action', 'No Action'),
    ]

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='disputes')
    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, related_name='disputes', null=True, blank=True)
    complainant = models.ForeignKey(User, on_delete=models.CASCADE, related_name='filed_disputes')
    defendant = models.ForeignKey(User, on_delete=models.CASCADE, related_name='disputes_against')

    title = models.CharField(max_length=200)
    description = models.TextField()
    reason = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')

    admin_notes = models.TextField(blank=True)
    resolution = models.TextField(blank=True)
    resolution_action = models.CharField(max_length=20, choices=RESOLUTION_CHOICES, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'نزاع'
        verbose_name_plural = 'النزاعات'

    def __str__(self):
        return f"Dispute #{self.id} - {self.title}"


class Report(models.Model):
    """A user's report that a listing, store, or another user violates the
    app's policies (counterfeit goods, scams, harassment, ...). Distinct
    from Dispute, which is a claim tied to one specific paid reservation —
    a Report has no reservation/financial angle and any authenticated user
    can file one against any item/store/user, not just a trading partner.

    Exactly one of reported_item/reported_store/reported_user is set (the
    report's target); enforced both by the CheckConstraint below and by
    CreateReportSerializer at the API layer.
    """
    REASON_CHOICES = [
        ('counterfeit', 'منتج مقلد أو مخالف'),
        ('prohibited', 'منتج ممنوع أو غير قانوني'),
        ('misleading', 'وصف مضلل للمنتج'),
        ('scam', 'احتيال أو نصب'),
        ('harassment', 'مضايقة أو إساءة'),
        ('spam', 'محتوى مزعج أو متكرر'),
        ('inappropriate', 'محتوى غير لائق'),
        ('fake_account', 'حساب أو متجر وهمي'),
        ('other', 'أخرى'),
    ]

    STATUS_CHOICES = [
        ('pending', 'قيد الانتظار'),
        ('reviewing', 'قيد المراجعة'),
        ('resolved', 'تم اتخاذ إجراء'),
        ('dismissed', 'مرفوض'),
    ]

    # What (if anything) the admin did about it — recorded on the report
    # itself so its history reads standalone, in addition to the note
    # resolve_report() appends to the target's own admin_notes.
    ACTION_CHOICES = [
        ('none', 'لا إجراء'),
        ('warning', 'تحذير للمُبلَغ عنه'),
        ('item_archived', 'أرشفة المنتج'),
        ('store_suspended', 'إيقاف المتجر'),
        ('user_suspended', 'تعليق الحساب'),
        ('reel_archived', 'أرشفة الريل'),
    ]

    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='filed_reports')

    reported_item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True, blank=True, related_name='reports')
    reported_store = models.ForeignKey(UserStore, on_delete=models.CASCADE, null=True, blank=True, related_name='reports')
    reported_user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='reports_against')
    # Forward reference — Reel is defined later in this file (after
    # LiveSessionBan), same as how Report itself precedes several other
    # models it doesn't need to reference by string.
    reported_reel = models.ForeignKey('Reel', on_delete=models.CASCADE, null=True, blank=True, related_name='reports')

    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    description = models.TextField(blank=True, help_text='تفاصيل إضافية اختيارية من المُبلِغ')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    action_taken = models.CharField(max_length=20, choices=ACTION_CHOICES, default='none')
    # Staff-only, same convention as Item.admin_notes/User.admin_notes.
    admin_notes = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_reports'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'بلاغ'
        verbose_name_plural = 'البلاغات'
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(reported_item__isnull=False, reported_store__isnull=True, reported_user__isnull=True, reported_reel__isnull=True) |
                    models.Q(reported_item__isnull=True, reported_store__isnull=False, reported_user__isnull=True, reported_reel__isnull=True) |
                    models.Q(reported_item__isnull=True, reported_store__isnull=True, reported_user__isnull=False, reported_reel__isnull=True) |
                    models.Q(reported_item__isnull=True, reported_store__isnull=True, reported_user__isnull=True, reported_reel__isnull=False)
                ),
                name='report_exactly_one_target',
            )
        ]

    def __str__(self):
        target = self.reported_item or self.reported_store or self.reported_reel or self.reported_user
        return f"Report #{self.id} - {target} ({self.get_reason_display()})"

    @property
    def target_type(self):
        if self.reported_item_id:
            return 'item'
        if self.reported_store_id:
            return 'store'
        if self.reported_reel_id:
            return 'reel'
        return 'user'

    @property
    def target(self):
        return self.reported_item or self.reported_store or self.reported_reel or self.reported_user


class DeliveryCompany(models.Model):
    """
    The delivery company products are handed off to. Deliberately thin —
    the platform does not model this company's drivers/dispatch internally;
    it hands off a DeliveryOrder with the info needed and the company does
    the rest. `account` is the one login the company uses to see its
    assigned orders and report back coarse status (picked up / arrived /
    failed) — not a per-driver access model.
    """
    name = models.CharField(max_length=200)
    account = models.OneToOneField(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='delivery_company',
        help_text="The single login this company uses to view/update its assigned deliveries"
    )
    primary_contact_name = models.CharField(max_length=200, blank=True)
    primary_contact_phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Flat-rate pricing: the company charges one price for a delivery that
    # stays within the seller's governorate, and another for a delivery that
    # crosses into a different one. Charged to the buyer at Reservation
    # completion — see ReservationViewSet.complete.
    intra_governorate_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text='رسم التوصيل داخل نفس محافظة البائع'
    )
    inter_governorate_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text='رسم التوصيل إلى محافظة مختلفة عن محافظة البائع'
    )
    # What الشورجة keeps out of this company's delivery fee. Negotiated per
    # company (each signs its own terms), so there is no platform-wide
    # default: a company nobody negotiated with keeps 100%, exactly as
    # before this field existed.
    platform_commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='نسبة عمولة المنصّة من رسم التوصيل لهذه الشركة (%)',
    )
    # Cap on combined Item.package_units a single delivery trip can carry —
    # see delivery_utils.pack_into_delivery_groups. A seller's reservations
    # that exceed this ship as more than one trip instead of being blocked.
    max_combined_units = models.PositiveIntegerField(
        default=6,
        help_text='أقصى مجموع "وحدات حجم" يمكن دمجها برحلة توصيل واحدة برسم واحد'
    )

    class Meta:
        verbose_name = 'شركة توصيل'
        verbose_name_plural = 'شركات التوصيل'

    def __str__(self):
        return self.name

    def outstanding_cash_liability(self):
        """Cash the company has confirmed collecting from buyers (COD) and
        still owes the platform — see CashSettlement.

        Sums cash_due_to_platform, not cash_to_collect: the company keeps
        its own delivery earnings out of the collected cash and hands over
        the remainder, so the gross figure would overstate the debt by the
        company's share forever."""
        total = self.delivery_orders.filter(
            cash_collected=True, cash_settlement__isnull=True
        ).aggregate(total=models.Sum('cash_due_to_platform'))['total']
        return total or Decimal('0.00')


class CashSettlement(models.Model):
    """
    A record of the delivery company physically handing the platform the
    cash it collected from buyers on the platform's behalf (COD
    remainders — see DeliveryOrder.cash_collected/cash_to_collect). Created
    only through DeliveryOrderAdmin's settle_cash_action, which batches
    every DeliveryOrder being cleared into one settlement at once — so
    there's an audit trail of exactly which deliveries were included in
    which payment, by whom, and when.
    """
    company = models.ForeignKey(DeliveryCompany, on_delete=models.CASCADE, related_name='cash_settlements')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    settled_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='cash_settlements_recorded'
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'تسوية نقدية'
        verbose_name_plural = 'التسويات النقدية'

    def __str__(self):
        return f'{self.company.name} — {self.amount} — {self.created_at:%Y-%m-%d}'


class DeliveryBatch(models.Model):
    """
    One physical delivery trip covering several reservations bundled
    together (same seller, same buyer, same delivery address, combined
    Item.package_units within the company's max_combined_units) — see
    delivery_utils.combine_deliveries. `combined_fee` is what's actually
    owed for the trip; each underlying DeliveryOrder still tracks its own
    reservation's status independently (a buyer can refuse one item and
    keep the rest), this just tags them as one trip so only one of them
    keeps a nonzero DeliveryOrder.delivery_fee/Reservation.delivery_fee —
    the others were already refunded (or excused from COD) the difference.
    """
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='delivery_batches')
    delivery_location = models.ForeignKey(
        Location, on_delete=models.SET_NULL, null=True, related_name='delivery_batches'
    )
    company = models.ForeignKey(
        DeliveryCompany, on_delete=models.SET_NULL, null=True, related_name='delivery_batches'
    )
    combined_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'دفعة توصيل مجمّعة'
        verbose_name_plural = 'دفعات التوصيل المجمّعة'

    def __str__(self):
        return f"Batch #{self.id} — {self.buyer.username} ({self.delivery_orders.count()} items)"


class DeliveryRate(models.Model):
    """One delivery price for a company: from a governorate, to a
    governorate, for a package size. Any of the three may be blank as a
    wildcard, and the most specific matching row wins — see
    delivery_utils.compute_delivery_fee for the fallback order. The
    company's two flat fees remain the last resort, so a company with no
    rate rows behaves exactly as before."""
    company = models.ForeignKey(DeliveryCompany, on_delete=models.CASCADE, related_name='rates')
    from_governorate = models.CharField(
        max_length=100, choices=IRAQ_GOVERNORATE_CHOICES, blank=True,
        help_text='فارغ = أي محافظة')
    to_governorate = models.CharField(
        max_length=100, choices=IRAQ_GOVERNORATE_CHOICES, blank=True,
        help_text='فارغ = أي محافظة')
    package_size = models.CharField(
        max_length=10, choices=PACKAGE_SIZE_CHOICES, blank=True,
        help_text='فارغ = أي حجم')
    fee = models.DecimalField(max_digits=10, decimal_places=2,
                              validators=[MinValueValidator(0)])
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'تعرفة توصيل'
        verbose_name_plural = 'تعرفة التوصيل'
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'from_governorate', 'to_governorate', 'package_size'],
                name='one_rate_per_route_and_size'),
        ]

    def __str__(self):
        return '%s → %s (%s): %s' % (self.from_governorate or '*', self.to_governorate or '*',
                                     self.package_size or '*', self.fee)

    @property
    def specificity(self):
        return int(bool(self.from_governorate)) + int(bool(self.to_governorate)) \
            + int(bool(self.package_size))


class DeliveryOrder(models.Model):
    """
    A single leg handed off to the delivery company: either the outbound
    trip (seller -> buyer) after the seller chooses to deliver, or a return
    trip (buyer -> seller) after a refused delivery or a dispute resolved
    in the buyer's favor. Contact/address fields are a snapshot taken at
    creation time, independent of the underlying Location later changing.

    Status lifecycle (outbound):
      assigned -> picked_up -> delivered (company handed item to buyer,
      triggers a "confirm or refuse receipt" prompt) -> confirmed (buyer
      pressed received — settles the reservation) or refused (buyer
      rejected at the door -> spawns a return leg).

    Status lifecycle (return):
      assigned -> picked_up (company took the item back from the buyer —
      this is what actually releases the refund, see DisputeViewSet.resolve
      and DeliveryOrderViewSet.refuse_receipt) -> delivered (handed back to
      the seller — administrative closure only, no financial effect).
    """
    STATUS_CHOICES = [
        ('assigned', 'Assigned to Company'),
        ('picked_up', 'Picked Up'),
        ('delivered', 'Delivered'),
        ('confirmed', 'Confirmed by Buyer'),
        ('refused', 'Refused by Buyer'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    DIRECTION_CHOICES = [
        ('outbound', 'Outbound — seller to buyer'),
        ('return', 'Return — buyer to seller'),
    ]

    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, related_name='delivery_orders')
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default='outbound')
    company = models.ForeignKey(
        DeliveryCompany, on_delete=models.SET_NULL, null=True, related_name='delivery_orders'
    )
    # Set only when this outbound leg was created as part of a combined
    # shipment — see delivery_utils.combine_deliveries. Null for every
    # ordinary single-reservation delivery, which is still the common case.
    batch = models.ForeignKey(
        DeliveryBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name='delivery_orders'
    )
    # Set only for return legs spawned by a dispute resolved 'refund_buyer' —
    # see DisputeViewSet.resolve. Refusal-triggered returns leave this null.
    dispute = models.ForeignKey(
        Dispute, on_delete=models.SET_NULL, null=True, blank=True, related_name='return_delivery_orders'
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='assigned')

    from_name = models.CharField(max_length=200)
    from_phone = models.CharField(max_length=20)
    from_address = models.TextField()
    from_lat = models.DecimalField(max_digits=11, decimal_places=6, null=True, blank=True)
    from_lng = models.DecimalField(max_digits=11, decimal_places=6, null=True, blank=True)
    # Snapshot of the pickup Location's governorate at creation time — lets
    # the delivery company filter/sort its queue by where each order starts
    # without parsing from_address. Blank when there was no Location on file.
    from_governorate = models.CharField(max_length=100, choices=IRAQ_GOVERNORATE_CHOICES, blank=True)

    to_name = models.CharField(max_length=200)
    to_phone = models.CharField(max_length=20)
    to_address = models.TextField()
    to_lat = models.DecimalField(max_digits=11, decimal_places=6, null=True, blank=True)
    to_lng = models.DecimalField(max_digits=11, decimal_places=6, null=True, blank=True)

    item_summary = models.CharField(max_length=255, help_text="Item title/description shown to the company")
    quantity = models.PositiveIntegerField(default=1)
    special_notes = models.TextField(blank=True)
    # Only meaningful on outbound legs with is_deposit_only reservations —
    # the amount the company must collect in cash from the buyer.
    cash_to_collect = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    # Set when the company confirms it physically collected cash_to_collect
    # from the buyer (bundled into mark_arrived — see DeliveryOrderViewSet).
    # This is what makes the remainder part of Reservation.amount_charged,
    # i.e. releasable to the seller — it is also the company's debt to the
    # platform until settled (reconciliation is a separate, later concern).
    cash_collected = models.BooleanField(default=False)
    cash_collected_at = models.DateTimeField(null=True, blank=True)
    # Set once this order's collected cash has been included in a
    # settlement batch with the company — see CashSettlement and
    # DeliveryOrderAdmin.settle_cash_action.
    cash_settlement = models.ForeignKey(
        CashSettlement, on_delete=models.SET_NULL, null=True, blank=True, related_name='delivery_orders'
    )
    # Snapshot of Reservation.delivery_fee at the moment this outbound leg
    # was created — the gross fee for this trip, before the platform's cut.
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    # The platform's cut of delivery_fee, frozen from
    # DeliveryCompany.platform_commission_percent when this leg was created —
    # same rule as the seller's commission: a later renegotiation never
    # changes a trip already dispatched.
    platform_commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'))
    platform_commission_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'))
    # What the company must actually hand over out of the cash it collected.
    #
    # The company nets its own earnings out of the cash on the spot — it
    # keeps (delivery_fee - platform_commission_amount) and hands the
    # platform the rest — so the platform never physically receives the
    # company's share and must not book it as owed. cash_to_collect stays
    # the GROSS figure the buyer pays; this is the net the books chase.
    # See DeliveryCompany.outstanding_cash_liability and
    # DeliveryOrderAdmin.settle_cash_action, which both use this field.
    cash_due_to_platform = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'))

    failure_reason = models.TextField(blank=True)

    assigned_at = models.DateTimeField(auto_now_add=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    refused_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-assigned_at']
        verbose_name = 'طلب توصيل'
        verbose_name_plural = 'طلبات التوصيل'

    def __str__(self):
        return f"DeliveryOrder #{self.id} ({self.direction}) - {self.item_summary}"


class Representative(models.Model):
    """Representative assignment for selling items"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='representatives')
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='items_represented_for')
    representative = models.ForeignKey(User, on_delete=models.CASCADE, related_name='representing_items')

    commission_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Commission percentage the representative will earn"
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Permissions
    can_list = models.BooleanField(default=True)
    can_negotiate = models.BooleanField(default=True)
    can_complete_sale = models.BooleanField(default=True)

    # Request details
    request_message = models.TextField(blank=True, help_text="Message from representative to owner")
    rejection_reason = models.TextField(blank=True, help_text="Reason for rejection if applicable")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ['item', 'representative']
        ordering = ['-created_at']
        verbose_name = 'مندوب'
        verbose_name_plural = 'المندوبون'

    def __str__(self):
        return f"{self.representative.username} representing {self.item.title} for {self.owner.username}"

    def is_active(self):
        """Check if representative assignment is active"""
        return self.status in ['accepted', 'active']


class Transaction(models.Model):
    """Financial transactions - ENHANCED"""
    TRANSACTION_TYPE_CHOICES = [
        ('deposit', 'Deposit'),
        ('payment', 'Payment'),
        ('refund', 'Refund'),
        ('commission', 'Commission'),
        ('penalty', 'Penalty'),
        ('withdrawal', 'Withdrawal'),
        ('storage_fee', 'Storage Fee'),  # NEW
        ('purchase', 'Purchase'),  # NEW
        ('payout', 'Payout'),  # NEW
        ('delivery_fee', 'Delivery Fee'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
        ('cancelled', 'Cancelled'),  # NEW
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Relationships
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    reservation = models.ForeignKey(Reservation, on_delete=models.SET_NULL, null=True, blank=True)
    auction = models.ForeignKey(AuctionListing, on_delete=models.SET_NULL, null=True, blank=True)
    transfer = models.ForeignKey(ItemTransfer, on_delete=models.SET_NULL, null=True, blank=True)  # NEW

    # Payment gateway
    stripe_payment_intent_id = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'معاملة مالية'
        verbose_name_plural = 'المعاملات المالية'


class Chat(models.Model):
    """Chat between users about an item, or directly with a store"""
    # item is optional: null when the chat is a direct store-level conversation
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='chats',
        null=True, blank=True
    )
    # store is set for direct store-level conversations (contact the store)
    store = models.ForeignKey(
        'UserStore', on_delete=models.CASCADE, related_name='chats',
        null=True, blank=True
    )
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='buyer_chats')
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='seller_chats')

    # Chat status
    is_active = models.BooleanField(default=True)
    is_archived_by_buyer = models.BooleanField(default=False)
    is_archived_by_seller = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['item', 'buyer', 'seller']
        ordering = ['-updated_at']
        verbose_name = 'محادثة'
        verbose_name_plural = 'المحادثات'

    def __str__(self):
        about = self.item.title if self.item else (self.store.name if self.store else 'store')
        return f"Chat: {self.buyer.username} - {self.seller.username} about {about}"

    def get_other_user(self, user):
        """Get the other participant in the chat"""
        return self.seller if user == self.buyer else self.buyer

    def get_unread_count(self, user):
        """Get unread message count for a specific user"""
        return self.messages.filter(is_read=False).exclude(sender=user).count()

    def mark_all_read(self, user):
        """Mark all messages as read for a specific user"""
        self.messages.exclude(sender=user).update(is_read=True)


class Message(models.Model):
    """Chat messages"""
    MESSAGE_TYPE_CHOICES = [
        ('user', 'User Message'),
        ('image', 'Image'),
        ('system', 'System Message'),
        ('offer', 'Price Offer'),
        ('counter_offer', 'Counter Offer'),
    ]

    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages', null=True, blank=True)
    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPE_CHOICES, default='user')
    content = models.TextField()

    # For offers — offer_amount is the total price for offer_quantity items
    # (not a per-unit price)
    offer_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    offer_quantity = models.PositiveIntegerField(null=True, blank=True, default=1)
    offer_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('accepted', 'Accepted'),
            ('rejected', 'Rejected'),
            ('expired', 'Expired'),
        ],
        null=True,
        blank=True
    )

    # Attachments
    attachment = models.FileField(upload_to='chat_attachments/', null=True, blank=True)

    is_read = models.BooleanField(default=False)
    is_deleted_by_sender = models.BooleanField(default=False)
    is_deleted_by_receiver = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'رسالة'
        verbose_name_plural = 'الرسائل'

    def __str__(self):
        return f"Message from {self.sender.username if self.sender else 'System'} in {self.chat}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Update chat's updated_at timestamp
        self.chat.updated_at = timezone.now()
        self.chat.save(update_fields=['updated_at'])


class Rating(models.Model):
    """User ratings"""
    RATING_TYPE_CHOICES = [
        ('buyer', 'As Buyer'),
        ('seller', 'As Seller'),
        ('representative', 'As Representative'),
    ]

    rater = models.ForeignKey(User, on_delete=models.CASCADE, related_name='given_ratings')
    rated_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_ratings')
    rating_type = models.CharField(max_length=20, choices=RATING_TYPE_CHOICES)
    score = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True, related_name='user_ratings')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['rater', 'rated_user', 'item', 'rating_type']
        verbose_name = 'تقييم مستخدم'
        verbose_name_plural = 'تقييمات المستخدمين'


class ProductReview(models.Model):
    """تقييم المنتج بنجوم وتعليق وصورة اختيارية"""
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='product_reviews_given')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='product_reviews')
    score = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    image = models.ImageField(upload_to='review_images/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['reviewer', 'item']
        ordering = ['-created_at']
        verbose_name = 'مراجعة منتج'
        verbose_name_plural = 'مراجعات المنتجات'

    def __str__(self):
        return f"{self.reviewer.username} → {self.item.title} ({self.score}★)"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.item.update_rating()

    def delete(self, *args, **kwargs):
        item = self.item
        super().delete(*args, **kwargs)
        item.update_rating()


class StoreReview(models.Model):
    """تقييم المتجر بنجوم وتعليق"""
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='store_reviews_given')
    store = models.ForeignKey(UserStore, on_delete=models.CASCADE, related_name='store_reviews')
    score = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['reviewer', 'store']
        ordering = ['-created_at']
        verbose_name = 'مراجعة متجر'
        verbose_name_plural = 'مراجعات المتاجر'

    def __str__(self):
        return f"{self.reviewer.username} → {self.store.name} ({self.score}★)"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.store.update_rating()

    def delete(self, *args, **kwargs):
        store = self.store
        super().delete(*args, **kwargs)
        store.update_rating()


class StoreFollow(models.Model):
    """A customer following a store (متابعة المتجر)"""
    follower = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='following_stores'
    )
    store = models.ForeignKey(
        UserStore, on_delete=models.CASCADE, related_name='followers'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['follower', 'store']
        ordering = ['-created_at']
        verbose_name = 'متابعة متجر'
        verbose_name_plural = 'متابعات المتاجر'

    def __str__(self):
        return f"{self.follower.username} follows {self.store.name}"


class StoreCoupon(models.Model):
    """A discount coupon a store gives to customers (كوبونات المتجر)"""
    store = models.ForeignKey(
        UserStore, on_delete=models.CASCADE, related_name='coupons'
    )
    code = models.CharField(max_length=40)
    # discount value in IQD (the amount shown on the coupon card)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        validators=[MinValueValidator(0)]
    )
    # minimum order value required to use the coupon (للطلبات فوق ...)
    min_order_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        validators=[MinValueValidator(0)]
    )
    description = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['store', 'code']
        ordering = ['-created_at']
        verbose_name = 'كوبون متجر'
        verbose_name_plural = 'كوبونات المتاجر'

    def __str__(self):
        return f"{self.code} ({self.amount} IQD) - {self.store.name}"

    @property
    def is_valid(self):
        """Active and not expired"""
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True


class CouponCollection(models.Model):
    """Records a customer collecting a coupon (جمع الكوبون)"""
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='collected_coupons'
    )
    coupon = models.ForeignKey(
        StoreCoupon, on_delete=models.CASCADE, related_name='collections'
    )
    collected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['user', 'coupon']
        ordering = ['-collected_at']
        verbose_name = 'كوبون مُجمَّع'
        verbose_name_plural = 'الكوبونات المُجمَّعة'

    def __str__(self):
        return f"{self.user.username} collected {self.coupon.code}"


class LiveSession(models.Model):
    """Store owner's live video shopping broadcast (البث المباشر)."""
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('live', 'Live'),
        ('ended', 'Ended'),
        ('cancelled', 'Cancelled'),
    ]

    store = models.ForeignKey(UserStore, on_delete=models.CASCADE, related_name='live_sessions')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    thumbnail = models.ImageField(upload_to='live_thumbnails/', null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')

    # Agora RTC channel name. Media never passes through Django — this is
    # only used to mint join tokens (see core/agora_utils.py). Generated at
    # creation time so scheduled sessions already have a stable channel.
    agora_channel_name = models.CharField(max_length=64, unique=True, blank=True, editable=False)

    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # Denormalized counters — there's no persisted row per anonymous WS
    # viewer to COUNT() over, so these are maintained directly by
    # LiveSessionConsumer/views via F() updates.
    current_viewer_count = models.PositiveIntegerField(default=0)
    peak_viewer_count = models.PositiveIntegerField(default=0)
    total_views = models.PositiveIntegerField(default=0)
    likes_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'جلسة بث مباشر'
        verbose_name_plural = 'جلسات البث المباشر'
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['status', '-current_viewer_count']),
            models.Index(fields=['status', '-likes_count']),
        ]

    def __str__(self):
        return f"{self.title} - {self.store.name} [{self.status}]"

    def save(self, *args, **kwargs):
        if not self.agora_channel_name:
            self.agora_channel_name = f"live_{uuid.uuid4().hex[:16]}"
        super().save(*args, **kwargs)

    def is_live(self):
        return self.status == 'live'


class LiveSessionItem(models.Model):
    """A catalog Item queued/shown during a LiveSession, with its own
    live-only price/deposit snapshot (independent of any FixedPriceListing)."""
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('now_showing', 'Now Showing'),
        ('sold_out', 'Sold Out'),
        ('skipped', 'Skipped'),
    ]

    session = models.ForeignKey(LiveSession, on_delete=models.CASCADE, related_name='items')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='live_session_entries')

    price = models.DecimalField(max_digits=10, decimal_places=2)
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2)

    order = models.PositiveIntegerField(default=0, help_text="Queue position")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    shown_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return f"{self.item.title} in {self.session.title} [{self.status}]"


class LiveSessionLike(models.Model):
    """A viewer liking a live session — mirrors StoreFollow's pattern."""
    session = models.ForeignKey(LiveSession, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='live_session_likes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['session', 'user']
        ordering = ['-created_at']
        verbose_name = 'إعجاب بث مباشر'
        verbose_name_plural = 'إعجابات البث المباشر'

    def __str__(self):
        return f"{self.user.username} likes {self.session.title}"


class LiveSessionComment(models.Model):
    """A viewer's live comment during a broadcast (تعليق مباشر). Persisted
    (not purely ephemeral) so a viewer who joins mid-stream can fetch recent
    history, and so the store owner can moderate (delete) abusive comments."""
    session = models.ForeignKey(LiveSession, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='live_session_comments')
    content = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'تعليق بث مباشر'
        verbose_name_plural = 'تعليقات البث المباشر'

    def __str__(self):
        return f"{self.user.username}: {self.content[:40]}"


class LiveSessionViewer(models.Model):
    """Tracks which signed-in users have watched a live session, upserted by
    LiveSessionConsumer on connect/disconnect. Anonymous viewers have no
    identity to track here — they still count toward
    LiveSession.current_viewer_count, they just can't appear in this list.
    active_connections counts simultaneous open sockets (multiple tabs/
    devices) so closing one doesn't flip is_watching off while another from
    the same user is still connected."""
    session = models.ForeignKey(LiveSession, on_delete=models.CASCADE, related_name='viewers')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='live_session_views')
    is_watching = models.BooleanField(default=True)
    active_connections = models.PositiveIntegerField(default=0)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['session', 'user']
        ordering = ['-last_seen_at']
        indexes = [models.Index(fields=['session', 'is_watching'])]
        verbose_name = 'مشاهد بث مباشر'
        verbose_name_plural = 'مشاهدو البث المباشر'

    def __str__(self):
        return f"{self.user.username} watching {self.session.title}"


class LiveSessionModerator(models.Model):
    """A user the store owner has granted moderator powers (delete comments,
    ban/unban viewers) for one specific live session — scoped to that session
    only, not the store's other broadcasts."""
    session = models.ForeignKey(LiveSession, on_delete=models.CASCADE, related_name='moderators')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='moderated_live_sessions')
    added_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['session', 'user']
        ordering = ['-created_at']
        verbose_name = 'مشرف بث مباشر'
        verbose_name_plural = 'مشرفو البث المباشر'

    def __str__(self):
        return f"{self.user.username} moderates {self.session.title}"


class LiveSessionBan(models.Model):
    """A viewer banned from one specific live session — can't post comments
    or reconnect to its WebSocket room. Scoped to this session only; it does
    not carry over to the store's other broadcasts."""
    session = models.ForeignKey(LiveSession, on_delete=models.CASCADE, related_name='bans')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='live_session_bans')
    banned_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['session', 'user']
        ordering = ['-created_at']
        verbose_name = 'حظر بث مباشر'
        verbose_name_plural = 'حظورات البث المباشر'

    def __str__(self):
        return f"{self.user.username} banned from {self.session.title}"


class Reel(models.Model):
    """Short vertical video posted by a store (ريلز) — TikTok/Instagram-Reels
    style shoppable video. Video/thumbnail bytes never touch Django: they're
    uploaded directly to the Cloudflare R2 bucket via presigned URLs
    (core/aws_utils.py, ReelViewSet.request_upload_url/finalize) and served
    for playback from that bucket's custom domain on Cloudflare's CDN.
    video_key/thumbnail_key store only the object key (never a full URL) so
    switching storage/CDN needs no migration — the S3 -> R2 move was exactly
    that: a new REELS_PUBLIC_BASE_URL, same keys. See video_url/thumbnail_url."""
    STATUS_CHOICES = [
        ('pending_upload', 'Pending Upload'),
        ('published', 'Published'),
        ('failed', 'Failed'),
        ('archived', 'Archived'),
        # Owner asked for a permanent delete but the reel had been reported,
        # so the row survives as a tombstone (media purged from S3, keys
        # blanked) to keep Report.reported_reel — a CASCADE FK — intact.
        # See ReelViewSet.destroy(). Hidden from both the feed and my_reels.
        ('deleted', 'Deleted'),
    ]

    store = models.ForeignKey(UserStore, on_delete=models.CASCADE, related_name='reels')
    # SET_NULL, not CASCADE: purge_archived_items (core/tasks.py) hard-deletes
    # Items that have sat archived for a year — a Reel tagging one of those
    # items must survive that purge instead of being silently wiped out with
    # it, so it just loses its product tag (item becomes None) and stays up.
    item = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True, related_name='reels'
    )
    caption = models.TextField(blank=True)

    # S3 object keys only — never a full URL. See video_url/thumbnail_url.
    video_key = models.CharField(max_length=500)
    thumbnail_key = models.CharField(max_length=500, blank=True)

    duration_seconds = models.PositiveIntegerField()
    # Trusted value from S3's HEAD response at finalize time, not whatever
    # the client claims.
    file_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)

    # pending_upload -> published/failed, or published -> archived (soft
    # delete — same convention as Item since commit df880d2).
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending_upload')

    view_count = models.PositiveIntegerField(default=0)
    likes_count = models.PositiveIntegerField(default=0)
    comments_count = models.PositiveIntegerField(default=0)

    # Staff-only, same convention as Item.admin_notes/User.admin_notes.
    admin_notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'ريل'
        verbose_name_plural = 'الريلز'
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['store', 'status']),
        ]

    def __str__(self):
        return f"Reel #{self.id} - {self.store.name} [{self.status}]"

    @property
    def video_url(self):
        # Imported locally (not at module scope) to keep this module free of
        # any import-time dependency on core.aws_utils.
        from . import aws_utils
        return aws_utils.object_url(self.video_key)

    @property
    def thumbnail_url(self):
        from . import aws_utils
        return aws_utils.object_url(self.thumbnail_key)


class ReelLike(models.Model):
    """A viewer liking a reel — mirrors LiveSessionLike's pattern."""
    reel = models.ForeignKey(Reel, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reel_likes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['reel', 'user']
        ordering = ['-created_at']
        verbose_name = 'إعجاب ريل'
        verbose_name_plural = 'إعجابات الريلز'

    def __str__(self):
        return f"{self.user.username} likes reel #{self.reel_id}"


class ReelComment(models.Model):
    """A viewer's comment on a reel — mirrors LiveSessionComment's pattern."""
    reel = models.ForeignKey(Reel, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reel_comments')
    content = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'تعليق ريل'
        verbose_name_plural = 'تعليقات الريلز'

    def __str__(self):
        return f"{self.user.username}: {self.content[:40]}"


class ReelView(models.Model):
    """Tracks which signed-in users have watched a reel (upserted by
    ReelViewSet.view). Anonymous views still bump Reel.view_count but have
    no identity to record here — user is nullable, and unique_together with
    a nullable FK is safe since both Django and MySQL treat each NULL as
    distinct, so it never collapses multiple anonymous rows together (in
    practice no row is ever created for an anonymous view at all — see
    ReelViewSet.view)."""
    reel = models.ForeignKey(Reel, on_delete=models.CASCADE, related_name='view_records')
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True, related_name='reel_views'
    )
    view_count = models.PositiveIntegerField(default=1)
    first_viewed_at = models.DateTimeField(auto_now_add=True)
    last_viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['reel', 'user']
        ordering = ['-last_viewed_at']
        verbose_name = 'مشاهدة ريل'
        verbose_name_plural = 'مشاهدات الريلز'

    def __str__(self):
        who = self.user.username if self.user else 'anonymous'
        return f"{who} watched reel #{self.reel_id}"


from django.db import models, transaction as db_transaction
from django.db.models import F
from django.conf import settings
from decimal import Decimal


class Wallet(models.Model):
    """One wallet per user. Balance is the single source of truth for spendable money."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='wallet'
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    # Money locked in pending withdrawals (can't be spent or withdrawn twice)
    held_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'محفظة'
        verbose_name_plural = 'المحافظ'

    def __str__(self):
        return f"{self.user.username} wallet: {self.balance}"

    @property
    def available_balance(self):
        """What the user can actually spend or withdraw right now."""
        return self.balance - self.held_balance

    def credit(self, amount):
        """Add money to balance. Atomic, row-locked. Call inside a transaction.atomic()."""
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Credit amount must be positive")
        Wallet.objects.filter(pk=self.pk).update(balance=F('balance') + amount)
        self.refresh_from_db(fields=['balance'])

    def debit(self, amount):
        """Remove money from balance. Checks available funds."""
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Debit amount must be positive")
        # Re-read with a lock to avoid race conditions
        wallet = Wallet.objects.select_for_update().get(pk=self.pk)
        if wallet.available_balance < amount:
            raise ValueError(
                f"Insufficient balance. Available: {wallet.available_balance}, requested: {amount}"
            )
        Wallet.objects.filter(pk=self.pk).update(balance=F('balance') - amount)
        self.refresh_from_db(fields=['balance'])

    def hold(self, amount):
        """Move money into held (e.g. while a withdrawal is pending approval)."""
        amount = Decimal(str(amount))
        wallet = Wallet.objects.select_for_update().get(pk=self.pk)
        if wallet.available_balance < amount:
            raise ValueError(
                f"Insufficient balance to hold. Available: {wallet.available_balance}, requested: {amount}"
            )
        Wallet.objects.filter(pk=self.pk).update(held_balance=F('held_balance') + amount)
        self.refresh_from_db(fields=['held_balance'])

    def release_hold(self, amount):
        """Release held money back to spendable (e.g. withdrawal rejected)."""
        amount = Decimal(str(amount))
        Wallet.objects.filter(pk=self.pk).update(held_balance=F('held_balance') - amount)
        self.refresh_from_db(fields=['held_balance'])

    def settle_hold(self, amount):
        """Withdrawal approved & paid out: remove from both held and balance."""
        amount = Decimal(str(amount))
        Wallet.objects.filter(pk=self.pk).update(
            held_balance=F('held_balance') - amount,
            balance=F('balance') - amount,
        )
        self.refresh_from_db(fields=['held_balance', 'balance'])


def get_or_create_wallet(user):
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


class Notification(models.Model):
    """Persisted notifications per user — retrieved on app login"""
    NOTIFICATION_TYPES = [
        ('new_bid', 'New Bid'),
        ('outbid', 'Outbid'),
        ('auction_won', 'Auction Won'),
        ('auction_ending_soon', 'Auction Ending Soon'),
        ('reservation_created', 'Reservation Created'),
        ('reservation_confirmed', 'Reservation Confirmed'),
        ('reservation_completed', 'Reservation Completed'),
        ('reservation_cancelled_by_seller', 'Reservation Cancelled by Seller'),
        ('reservation_cancelled_by_buyer', 'Reservation Cancelled by Buyer'),
        ('reservation_expiring', 'Reservation Expiring'),
        ('payment_release_requested', 'Payment Release Requested'),
        ('new_message', 'New Message'),
        ('payment_successful', 'Payment Successful'),
        ('payment_failed', 'Payment Failed'),
        ('refund_processed', 'Refund Processed'),
        ('dispute_created', 'Dispute Created'),
        ('item_transfer', 'Item Transfer'),
        ('representative_assigned', 'Representative Assigned'),
        ('live_stream_started', 'Live Stream Started'),
        ('live_item_reserved', 'Live Item Reserved'),
        ('live_item_sold_out', 'Live Item Sold Out'),
        ('report_submitted', 'Report Submitted'),
        ('report_reviewed', 'Report Reviewed'),
        ('general', 'General'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    notification_type = models.CharField(max_length=50, choices=NOTIFICATION_TYPES, default='general')
    title = models.CharField(max_length=255)
    body = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'إشعار'
        verbose_name_plural = 'الإشعارات'

    def __str__(self):
        return f"Notification({self.user.username}): {self.title}"


class DepositRequest(models.Model):
    """User charges their balance from the app's Mastercard (simulated)."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='deposits'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    # Simulated card details (store ONLY last 4 in real life — never full PAN)
    card_last4 = models.CharField(max_length=4, blank=True)
    reference = models.CharField(max_length=64, blank=True)  # fake gateway ref
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'طلب إيداع'
        verbose_name_plural = 'طلبات الإيداع'

    def __str__(self):
        return f"Deposit {self.amount} by {self.user.username} [{self.status}]"


class WithdrawalRequest(models.Model):
    """User cashes out balance to their own Mastercard. Needs admin approval."""
    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved / Paid'),
        ('rejected', 'Rejected'),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='withdrawals'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    card_last4 = models.CharField(max_length=4, blank=True)  # destination card
    admin_notes = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reviewed_withdrawals'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'طلب سحب'
        verbose_name_plural = 'طلبات السحب'

    def __str__(self):
        return f"Withdrawal {self.amount} by {self.user.username} [{self.status}]"


# =============================================================================
# الوثائق القانونية (سياسة الخصوصية / شروط الاستخدام) + تسجيل موافقة المستخدم
# -----------------------------------------------------------------------------
# كل (نوع + لغة) له سلسلة إصدارات متصاعدة؛ "النسخة الحالية" = أعلى إصدار منشور.
# تُسجَّل موافقة كل مستخدم على إصدار محدّد (بوقت و IP) لأغراض الإثبات القانوني.
# =============================================================================
class LegalDocument(models.Model):
    """وثيقة قانونية منشورة ومُصدَّرة (سياسة خصوصية أو شروط استخدام)."""

    DOC_PRIVACY = 'privacy'
    DOC_TERMS = 'terms'
    DOC_TYPE_CHOICES = [
        (DOC_PRIVACY, 'سياسة الخصوصية'),
        (DOC_TERMS, 'شروط الاستخدام'),
    ]

    LANG_AR = 'ar'
    LANG_EN = 'en'
    LANG_CHOICES = [
        (LANG_AR, 'العربية'),
        (LANG_EN, 'English'),
    ]

    doc_type = models.CharField('النوع', max_length=20, choices=DOC_TYPE_CHOICES)
    language = models.CharField('اللغة', max_length=5, choices=LANG_CHOICES, default=LANG_AR)
    version = models.PositiveIntegerField(
        'الإصدار', default=1,
        help_text='رقم إصدار متصاعد لكل (نوع + لغة). أعلى إصدار منشور هو الحالي.',
    )
    title = models.CharField('العنوان', max_length=200)
    content = models.TextField(
        'المحتوى (HTML)',
        help_text='محتوى الوثيقة بصيغة HTML — يُعرض كما هو في التطبيق والصفحة العلنية.',
    )
    summary = models.CharField(
        'ملخّص التغيير', max_length=300, blank=True,
        help_text='سبب/ملخّص هذا الإصدار يُعرض للمستخدم عند طلب الموافقة (اختياري).',
    )
    effective_at = models.DateTimeField('تاريخ السريان', default=timezone.now)
    is_published = models.BooleanField('منشور', default=True)
    requires_reacceptance = models.BooleanField(
        'يتطلب موافقة جديدة', default=True,
        help_text='إذا كان هذا الإصدار يُلزم المستخدمين الحاليين بإعادة الموافقة.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'وثيقة قانونية'
        verbose_name_plural = 'الوثائق القانونية'
        ordering = ['doc_type', 'language', '-version']
        constraints = [
            models.UniqueConstraint(
                fields=['doc_type', 'language', 'version'],
                name='uniq_legal_doc_type_lang_version',
            ),
        ]

    def __str__(self):
        return f"{self.get_doc_type_display()} [{self.language}] v{self.version}"

    @classmethod
    def current(cls, doc_type, language=LANG_AR):
        """أحدث إصدار منشور لنوع/لغة معيّنين، أو None."""
        return (
            cls.objects.filter(
                doc_type=doc_type, language=language, is_published=True
            )
            .order_by('-version')
            .first()
        )


class LegalAcceptance(models.Model):
    """سجلّ موافقة مستخدم على إصدار محدّد من وثيقة قانونية (دليل إثبات)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='legal_acceptances',
    )
    # PROTECT: لا يُحذف إصدار سبق أن وافق عليه مستخدمون — يبقى الأثر القانوني.
    document = models.ForeignKey(
        LegalDocument, on_delete=models.PROTECT, related_name='acceptances',
    )
    accepted_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    app_version = models.CharField(max_length=40, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = 'موافقة قانونية'
        verbose_name_plural = 'الموافقات القانونية'
        ordering = ['-accepted_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'document'],
                name='uniq_user_legal_document_acceptance',
            ),
        ]

    def __str__(self):
        return f"{self.user_id} ✓ {self.document}"

class PaymentIntent(models.Model):
    """One attempt to take money from a customer through a payment gateway.

    Provider-agnostic on purpose: `provider` names which gateway handled it and
    the two `provider_*` columns hold whatever references that gateway issued.
    Adding a second gateway adds rows here, not columns.

    `purpose` is what the money is for. Only wallet top-up exists today —
    reservations are already paid out of the wallet balance — but paying for an
    order directly is the obvious next one, so the column exists rather than
    being retrofitted later.

    `applied_at` is the idempotency guard. A successful payment can be reported
    twice (webhook plus the app polling on return), and crediting a wallet twice
    is the worst bug this feature could have. Money moves only in the
    transition to `applied_at IS NOT NULL`, under a row lock.
    """

    STATE_PENDING = 'pending'
    STATE_SUCCEEDED = 'succeeded'
    STATE_FAILED = 'failed'
    STATE_CANCELLED = 'cancelled'
    STATE_CHOICES = [
        (STATE_PENDING, 'Pending'),
        (STATE_SUCCEEDED, 'Succeeded'),
        (STATE_FAILED, 'Failed'),
        (STATE_CANCELLED, 'Cancelled'),
    ]

    PURPOSE_WALLET_TOPUP = 'wallet_topup'
    PURPOSE_CHOICES = [
        (PURPOSE_WALLET_TOPUP, 'شحن المحفظة'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='payment_intents'
    )
    provider = models.CharField(max_length=32, db_index=True)
    purpose = models.CharField(
        max_length=32, choices=PURPOSE_CHOICES, default=PURPOSE_WALLET_TOPUP
    )

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=8, default='IQD')
    description = models.CharField(max_length=255, blank=True)

    # Our own id for this payment, sent to the gateway as its order reference
    # and the only thing we trust out of a webhook body.
    reference = models.CharField(max_length=64, unique=True, db_index=True)

    state = models.CharField(
        max_length=16, choices=STATE_CHOICES, default=STATE_PENDING, db_index=True
    )
    provider_payment_id = models.CharField(max_length=128, blank=True, db_index=True)
    provider_token = models.CharField(max_length=255, blank=True)
    checkout_url = models.URLField(max_length=500, blank=True)

    # Last authenticated read from the gateway. Kept for support and disputes;
    # never the basis of a decision on its own.
    last_provider_response = models.JSONField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    deposit = models.OneToOneField(
        'DepositRequest', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payment_intent'
    )
    applied_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'عملية دفع'
        verbose_name_plural = 'عمليات الدفع'
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['provider', 'state']),
        ]

    def __str__(self):
        return "Payment %s %s %s via %s [%s]" % (
            self.reference, self.amount, self.currency, self.provider, self.state
        )

    @property
    def is_final(self):
        return self.state in (self.STATE_SUCCEEDED, self.STATE_FAILED,
                              self.STATE_CANCELLED)
