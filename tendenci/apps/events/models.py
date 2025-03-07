from builtins import str
import uuid
from hashlib import md5
import operator
from datetime import datetime, timedelta
from dateutil.parser import parse
from functools import reduce
from django.conf import settings
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.urls import reverse
from django.db.models.aggregates import Sum
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.models import User
from django.template.defaultfilters import slugify
from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db.models.fields import AutoField
from django.contrib.contenttypes.fields import GenericRelation
from django.db.models import Q

from tagging.fields import TagField
from timezone_field import TimeZoneField

from tendenci.apps.events.managers import EventManager, RegistrantManager, EventTypeManager
from tendenci.apps.event_logs.models import EventLog
from tendenci.apps.notifications import models as notification
from tendenci.apps.perms.object_perms import ObjectPermission
from tendenci.apps.perms.models import TendenciBaseModel
from tendenci.apps.perms.utils import get_notice_recipients
from tendenci.apps.meta.models import Meta as MetaTags
from tendenci.apps.events.module_meta import EventMeta
from tendenci.apps.user_groups.models import Group
from tendenci.apps.user_groups.utils import get_default_group
from tendenci.apps.user_groups.models import GroupMembership

from tendenci.apps.invoices.models import Invoice
from tendenci.apps.files.models import File
from tendenci.apps.site_settings.utils import get_setting
from tendenci.apps.payments.models import PaymentMethod as GlobalPaymentMethod

from tendenci.apps.events.settings import (
    FIELD_MAX_LENGTH, LABEL_MAX_LENGTH, FIELD_TYPE_CHOICES, USER_FIELD_CHOICES, FIELD_FUNCTIONS)
from tendenci.apps.base.utils import (localize_date, get_timezone_choices,
    format_datetime_range)
from tendenci.apps.emails.models import Email
from tendenci.libs.boto_s3.utils import set_s3_file_permission
from tendenci.libs.abstracts.models import OrderingBaseModel
from tendenci.apps.trainings.models import Course, Certification

# from south.modelsinspector import add_introspection_rules
# add_introspection_rules([], [r'^timezone_field\.TimeZoneField'])

EMAIL_DEFAULT_ONLY = 'default'
EMAIL_CUSTOM_ONLY = 'custom'
EMAIL_BOTH = 'both'
REGEMAIL_TYPE_CHOICES = (
    (EMAIL_DEFAULT_ONLY, _('Default Email Only')),
    (EMAIL_CUSTOM_ONLY, _('Custom Email Only')),
    (EMAIL_BOTH, _('Default and Custom Email')),
)


class TypeColorSet(models.Model):
    """
    Colors representing a type [color-scheme]
    The values can be hex or literal color names
    """
    fg_color = models.CharField(max_length=20)
    bg_color = models.CharField(max_length=20)
    border_color = models.CharField(max_length=20)

    class Meta:
        app_label = 'events'

    def __str__(self):
        return '%s #%s' % (self.pk, self.bg_color)


class Type(models.Model):
    """
    Types is a way of grouping events
    An event can only be one type
    A type can have multiple events
    """
    name = models.CharField(max_length=50)
    slug = models.SlugField(max_length=50, editable=False)
    color_set = models.ForeignKey('TypeColorSet', on_delete=models.CASCADE)

    objects = EventTypeManager()

    class Meta:
        app_label = 'events'

    @property
    def fg_color(self):
        return '#%s' % self.color_set.fg_color

    @property
    def bg_color(self):
        return '#%s' % self.color_set.bg_color

    @property
    def border_color(self):
        return '#%s' % self.color_set.border_color

    def __str__(self):
        return self.name

    def event_count(self):
        return self.event_set.count()

    def save(self, *args, **kwargs):
        self.slug = slugify(self.name)
        super(Type, self).save(*args, **kwargs)


class Place(models.Model):
    """
    Event Place (location)
    An event can only be in one place
    A place can be used for multiple events
    """
    _original_name = None

    virtual = models.BooleanField(default=False, help_text=_('Is it a virtual event?'))
    name = models.CharField(max_length=150, blank=True)
    description = models.TextField(blank=True)

    # offline location
    address = models.CharField(max_length=150, blank=True)
    city = models.CharField(max_length=150, blank=True)
    state = models.CharField(max_length=150, blank=True)
    zip = models.CharField(max_length=150, blank=True)
    county = models.CharField(_('county'), max_length=50, blank=True)
    country = models.CharField(max_length=150, blank=True)
    national = models.BooleanField(default=False, help_text=_('Is it a national event?'))

    # online location
    url = models.URLField(blank=True)

    class Meta:
        app_label = 'events'

    def __init__(self, *args, **kwargs):
        super(Place, self).__init__(*args, **kwargs)
        self._original_name = self.name

    def __str__(self):
        str_place = '%s %s %s %s %s' % (
            self.name, self.address, ', '.join(self.city_state()), self.zip, self.country)
        return str(str_place.strip())

    def city_state(self):
        return [s for s in (self.city, self.state) if s]


class CEUCategory(models.Model):
    """
    Continuing Education Units
    Can optionally be included in Events
    """
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=15)
    parent = models.ForeignKey('self', null=True, on_delete=models.CASCADE)

    class Meta:
        verbose_name = _("Continuing Education Unit Category")
        verbose_name_plural = _("Continuing Education Unit Categories")
        ordering = ('name',)
        app_label = 'events'

    def __str__(self):
        return self.name


class EventCredit(models.Model):
    """Credits configured for an Event"""
    event = models.ManyToManyField('Event', blank=True)
    # When deleting a configuration for a credit, it will remove it from the event
    # configuration. History of credits earned for a Registrant will be saved
    # independent so it won't be lost if a CEUCategory is deleted.
    ceu_subcategory = models.ForeignKey(CEUCategory, on_delete=models.CASCADE)
    credit_count = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    alternate_ceu_id = models.CharField(max_length=150, blank=True, null=True)
    available = models.BooleanField(default=False)

    def save(self, apply_changes_to='self', from_event=None, *args, **kwargs):
        """Update for recurring events after save"""
        super().save(*args, **kwargs)
        if apply_changes_to != 'self':
            if not from_event:
                raise Exception("Must provide event to update recurring events from")

            recurring_events = from_event.recurring_event.event_set.all()

            if apply_changes_to == 'rest':
                recurring_events = recurring_events.filter(
                    start_dt__gte=from_event.start_dt)

            # Update existing configs. Create new ones if needed.
            for event in recurring_events:
                credit = event.get_or_create_credit_configuration(self.ceu_subcategory.pk, True)
                credit.credit_count = self.credit_count
                credit.alternate_ceu_id = self.alternate_ceu_id
                credit.available = self.available
                credit.save()


class EventStaff(models.Model):
    """Staff supporting Event"""
    event = models.ManyToManyField('Event')
    name = models.CharField(_('Name'), max_length=255)
    role = models.CharField(_('Role'), max_length=255)
    include_on_certificate = models.BooleanField(
        _('Include on Certificate'),
        default=True,
        help_text=_("Check to display name and role on certificate")
    )


class RegistrationConfiguration(models.Model):
    """
    Event registration
    Extends the event model
    """

    # TODO: use shorter name
    # TODO: do not use fixtures, use RAWSQL to prepopulate
    # TODO: set widget here instead of within form class

    BIND_TRUE = True
    BIND_FALSE = False

    BIND_CHOICES = (
        (True, _('Use one form for all pricings')),
        (False, _('Use separate form for each pricing'),
    ))

    payment_method = models.ManyToManyField(GlobalPaymentMethod)
    payment_required = models.BooleanField(
        help_text=_('A payment required before registration is accepted.'), default=True)
    external_payment_link = models.URLField(_('External payment link'),
                blank=True, default='',
                help_text=_('A third party payment link. If specified, online payment will be redirected to it.'))

    limit = models.IntegerField(_('Registration Limit'), default=0)
    enabled = models.BooleanField(_('Enable Registration'), default=False)

    require_guests_info = models.BooleanField(_('Require Guests Info'), help_text=_("If checked, " +
                        "the required fields in registration form are also required for guests.  "),
                        default=False)

    allow_guests = models.BooleanField(default=False)
    guest_limit = models.PositiveSmallIntegerField(default=0)
    is_guest_price = models.BooleanField(_('Guests Pay Registrant Price'), default=False)
    discount_eligible = models.BooleanField(default=True)
    gratuity_enabled = models.BooleanField(default=False)
    gratuity_options = models.CharField(_('Gratuity Options'),
                                     max_length=100,
                                     blank=True,
                                     default='17%,18%,19%,20%',
                                     help_text=_('Comma separated numeric numbers in percentage. '+
                                                 'A "%" will be appended if the percent sign is not present.'))
    gratuity_custom_option = models.BooleanField(_('Allow users to set their own gratuity'), default=False)
    allow_free_pass = models.BooleanField(default=False)
    display_registration_stats = models.BooleanField(_('Publicly Show Registration Stats'), default=False, help_text='Display the number of spots registered and the number of spots left to the public.')

    # custom reg form
    use_custom_reg_form = models.BooleanField(_('Use Custom Registration Form'), default=False)
    reg_form = models.ForeignKey("CustomRegForm", blank=True, null=True,
                                 verbose_name=_("Custom Registration Form"),
                                 related_name='regconfs',
                                 help_text=_("You'll have the chance to edit the selected form"),
                                 on_delete=models.CASCADE)
    # a custom reg form can be bound to either RegistrationConfiguration or RegConfPricing
    bind_reg_form_to_conf_only = models.BooleanField(_(' '),
                                 choices=BIND_CHOICES,
                                 default=BIND_TRUE)

    # base email for reminder email
    email = models.ForeignKey(Email, null=True, on_delete=models.SET_NULL)
    send_reminder = models.BooleanField(_('Send Email Reminder to attendees'), default=False)
    reminder_days = models.CharField(_('Specify when (? days before the event ' +
                                       'starts) the reminder should be sent '),
                                     max_length=20,
                                     null=True, blank=True,
                                     help_text=_('Comma delimited. Ex: 7,1'))

    registration_email_type = models.CharField(_('Registration Email'),
                                            max_length=20,
                                            choices=REGEMAIL_TYPE_CHOICES,
                                            default=EMAIL_DEFAULT_ONLY)
    registration_email_text = models.TextField(_('Registration Email Text'), blank=True)
    reply_to = models.EmailField(_('Registration email reply to'), max_length=120, null=True, blank=True,
                                 help_text=_('The email address that receives the reply message when registrants reply their registration confirmation emails.'))

    create_dt = models.DateTimeField(auto_now_add=True)
    update_dt = models.DateTimeField(auto_now=True)

    cancel_by_dt = models.DateTimeField(_('Cancel by'),
                                        blank=True,
                                        null=True)
    cancellation_fee = models.DecimalField(_('Cancellation Fee'),
                                           max_digits=21,
                                           decimal_places=2,
                                           default=0)
    cancellation_percent = models.DecimalField(_('Cancellation Percent'),
                                               default=0,
                                               max_digits=2,
                                               decimal_places=2,
                                               validators=[MinValueValidator(0), MaxValueValidator(1)])
    class Meta:
        app_label = 'events'

    @property
    def can_pay_online(self):
        """
        Check online payment dependencies.
        Return boolean.
        """
        has_method = GlobalPaymentMethod.objects.filter(is_online=True).exists()
        has_account = get_setting('site', 'global', 'merchantaccount') != ''
        has_api = any([settings.MERCHANT_LOGIN, settings.PAYPAL_MERCHANT_LOGIN])

        return all([has_method, has_account, has_api])

    def get_cancellation_fee(self, amount):
        """Get cancellation fee"""
        cancellation_fee = self.cancellation_fee

        if self.cancellation_percent:
            cancellation_fee = round(amount * self.cancellation_percent, 2)

        return cancellation_fee

    def get_available_pricings(self, user, is_strict=False, spots_available=-1):
        """
        Get the available pricings for this user.
        """
        filter_and, filter_or = RegConfPricing.get_access_filter(user,
                                                                 is_strict=is_strict,
                                                                 spots_available=spots_available)

        q_obj = None
        if filter_and:
            q_obj = Q(**filter_and)
        if filter_or:
            q_obj_or = reduce(operator.or_, [Q(**{key: value}) for key, value in filter_or.items()])
            if q_obj:
                q_obj = reduce(operator.and_, [q_obj, q_obj_or])
            else:
                q_obj = q_obj_or
        pricings = RegConfPricing.objects.filter(
                    reg_conf=self,
                    status=True
                    )
        if q_obj:
            pricings = pricings.filter(q_obj).distinct()
            
        # check and update spots_taken
        for pricing in pricings:
            if pricing.registration_cap:
                pricing.update_spots_taken()

        return pricings

    def has_member_price(self):
        """
        Returns [boolean] whether or not this
        event has a member price available.
        """
        price_set = self.regconfpricing_set.all()
        has_members = [p.allow_member for p in price_set]
        return any(has_members)


class RegConfPricing(OrderingBaseModel):
    """
    Registration configuration pricing
    """
    reg_conf = models.ForeignKey(RegistrationConfiguration, blank=True, null=True, on_delete=models.CASCADE)

    title = models.CharField(_('Pricing display name'), max_length=500, blank=True)
    description = models.TextField(_("Pricing description"), blank=True)
    quantity = models.IntegerField(_('Number of attendees'), default=1, blank=True, help_text='Total people included in each registration for this pricing group. Ex: Table or Team.')
    registration_cap = models.IntegerField(_('Registration limit'),
                                               default=0,
                                               help_text=_('The maximum number of registrants ' + \
                            'allowed for this pricing. 0 indicates unlimited. ' + \
                            'Note: this number should not exceed the specified registration limit.'))
    spots_taken = models.IntegerField(default=0)
    groups = models.ManyToManyField(Group, blank=True)
    days_price_covers = models.PositiveSmallIntegerField(
        blank=True, null=True, help_text=_("Number of days this price covers (optional)."))
    price = models.DecimalField(_('Price'), max_digits=21, decimal_places=2, default=0)
    include_tax = models.BooleanField(default=False)
    tax_rate = models.DecimalField(blank=True, max_digits=5, decimal_places=4, default=0,
                                   help_text=_('Example: 0.0825 for 8.25%.'))
    payment_required = models.BooleanField(
        help_text=_('A payment required before registration is accepted.'), default=False)

    reg_form = models.ForeignKey("CustomRegForm", blank=True, null=True,
                                 verbose_name=_("Custom Registration Form"),
                                 related_name='regconfpricings',
                                 help_text=_("You'll have the chance to edit the selected form"),
                                 on_delete=models.CASCADE)

    start_dt = models.DateTimeField(_('Start Date'))
    end_dt = models.DateTimeField(_('End Date'))

    allow_anonymous = models.BooleanField(_("Public can use this pricing"), default=False)
    allow_user = models.BooleanField(_("Signed in user can use this pricing"), default=False)
    allow_member = models.BooleanField(_("All members can use this pricing"), default=False)

    status = models.BooleanField(default=True)

    class Meta:
        app_label = 'events'

    def delete(self, *args, **kwargs):
        """
        Note that the delete() method for an object is not necessarily
        called when deleting objects in bulk using a QuerySet.
        """
        #print("%s, %s" % (self, "status set to false" ))
        self.status = False
        self.save(*args, **kwargs)

    def __str__(self):
        if self.title:
            return '%s' % self.title
        return '%s' % self.pk

    @property
    def requires_attendance_dates(self):
        """
        Pricing requires attendance dates if
        the Event requires attendance dates and
        days price covers is specified.
        """
        return (
            self.days_price_covers and
            self.reg_conf and hasattr(self.reg_conf, 'event') and
            self.reg_conf.event.requires_attendance_dates
        )

    def available(self):
        if not self.reg_conf.enabled or not self.status:
            return False
        if hasattr(self, 'event'):
            if localize_date(datetime.now()) > localize_date(self.event.end_dt, from_tz=self.timezone):
                return False
        return True

    def get_spots_status(self):
        """
        Return a tuple of (spots_taken, spots_available) for this pricing.
        """
        payment_required = self.reg_conf.payment_required or self.payment_required

        params = {
            'cancel_dt__isnull': True,
            'pricing_id': self.id
        }

        if payment_required:
            params['registration__invoice__balance'] = 0

        spots_taken = Registrant.objects.filter(**params).count()

        if self.registration_cap == 0:  # no limit
            return (spots_taken, -1)

        if spots_taken >= self.registration_cap:
            return (spots_taken, 0)

        return (spots_taken, self.registration_cap-spots_taken)

    def spots_available(self):
        return self.get_spots_status()[1]

    def update_spots_taken(self):
        payment_required = self.reg_conf.payment_required or self.payment_required

        params = {
            'cancel_dt__isnull': True,
            'pricing_id': self.id
        }

        if payment_required:
            params['registration__invoice__balance'] = 0

        spots_taken = Registrant.objects.filter(**params).count()
        if spots_taken != self.spots_taken:
            self.spots_taken = spots_taken
            self.save(update_fields=['spots_taken'])
        

    @property
    def registration_has_started(self):
        if localize_date(datetime.now()) >= localize_date(self.start_dt, from_tz=self.timezone):
            return True
        return False

    @property
    def registration_has_ended(self):
        if localize_date(datetime.now()) >= localize_date(self.end_dt, from_tz=self.timezone):
            return True
        return False

    @property
    def registration_has_recently_ended(self):
        if localize_date(datetime.now()) >= localize_date(self.end_dt, from_tz=self.timezone):
            delta = localize_date(datetime.now()) - localize_date(self.end_dt, from_tz=self.timezone)
            # Only include events that is within the 1-2 days window.
            if delta > timedelta(days=2):
                return False
            return True
        return False

    @property
    def is_open(self):
        status = [
            self.reg_conf.enabled,
            self.within_time,
        ]
        return all(status)

    @property
    def within_time(self):
        if localize_date(self.start_dt, from_tz=self.timezone) \
            <= localize_date(datetime.now())                    \
            <= localize_date(self.end_dt, from_tz=self.timezone):
            return True
        return False

    @property
    def timezone(self):
        return self.reg_conf.event.timezone.zone

    @staticmethod
    def get_access_filter(user, is_strict=False, spots_available=-1):
        if user.profile.is_superuser: return None, None
        now = datetime.now()
        filter_and, filter_or = None, None

        # Hide non-member pricing if setting turned on
        is_strict = is_strict or get_setting('module', 'events', 'hide_member_pricing')

        if is_strict:
            if user.is_anonymous:
                filter_or = {'allow_anonymous': True}
            elif not user.profile.is_member:
                filter_or = {'allow_anonymous': True,
                             'allow_user': True
                            }
            else:
                # user is a member
                filter_or = {'allow_anonymous': True,
                             'allow_user': True,
                             'allow_member': True}

        else:
            filter_or = {'allow_anonymous': True,
                        'allow_user': True,
                        'allow_member': True}
        if not user.is_anonymous and user.profile.is_member:
            # get a list of groups for this user
            groups_id_list = user.group_member.values_list('group__id', flat=True)
            if groups_id_list:
                filter_or.update({'groups__in': groups_id_list})

        filter_and = {'start_dt__lt': now,
                      'end_dt__gt': now,
                      }

        if spots_available != -1:
            if not user.profile.is_superuser:
                filter_and['quantity__lte'] = spots_available

        return filter_and, filter_or
    
    @property
    def tax_amount(self):
        if self.include_tax:
            return round(self.tax_rate * self.price, 2)
        return 0

    def target_display(self):
        target_str = ''

        if self.quantity > 1:
            if not target_str:
                target_str = 'for '
            else:
                target_str += ' - '
            target_str += 'a team of %d' % self.quantity

        return target_str


class Registration(models.Model):

    guid = models.TextField(max_length=40, editable=False)
    note = models.TextField(blank=True)
    event = models.ForeignKey('Event', on_delete=models.CASCADE)
    invoice = models.ForeignKey(Invoice, blank=True, null=True, on_delete=models.SET_NULL)

    # This field will not be used if dynamic pricings are enabled for registration
    # The pricings should then be found in the Registrant instances
    reg_conf_price = models.ForeignKey(RegConfPricing, null=True, on_delete=models.SET_NULL)

    reminder = models.BooleanField(default=False)
    
    key_contact_name = models.CharField(_('Training Contact'),
                                        max_length=150,
                                        blank=True,
                                        default='')
    key_contact_phone = models.CharField(_('Training Contact Phone'),
                                        max_length=50,
                                        blank=True,
                                        default='')
    key_contact_fax = models.CharField(_('Training Contact Fax'),
                                        max_length=50,
                                        blank=True,
                                        default='')
    need_reservation = models.BooleanField(default=False)
    nights = models.PositiveSmallIntegerField(default=0, blank=True,)
    begin_dt = models.DateField(_("Beginning on"), blank=True, null=True)
    

    # TODO: Payment-Method must be soft-deleted
    # so that it may always be referenced
    payment_method = models.ForeignKey(GlobalPaymentMethod, null=True, on_delete=models.SET_NULL)
    amount_paid = models.DecimalField(_('Amount Paid'), max_digits=21, decimal_places=2)
    gratuity = models.DecimalField(blank=True, default=0, max_digits=6, decimal_places=4)

    is_table = models.BooleanField(_('Is table registration'), default=False)
    # used for table
    quantity = models.IntegerField(_('Number of registrants for a table'), default=1)
    # admin price override for table
    override_table = models.BooleanField(_('Admin Price Override?'), default=False)
    override_price_table = models.DecimalField(_('Override Price'), max_digits=21,
                                         decimal_places=2,
                                         blank=True,
                                         default=0)
    canceled = models.BooleanField(_('Canceled'), default=False)

    creator = models.ForeignKey(User, related_name='created_registrations', null=True, on_delete=models.SET_NULL)
    owner = models.ForeignKey(User, related_name='owned_registrations', null=True, on_delete=models.SET_NULL)
    create_dt = models.DateTimeField(auto_now_add=True)
    update_dt = models.DateTimeField(auto_now=True)

    # addons text holder
    # will contain addons added in 'text format' by a user on event registration
    # will be added when creating the registration
    addons_added = models.TextField(null=True, blank=True)

    class Meta:
#         permissions = (("view_registration",_("Can view registration")),)
        app_label = 'events'

    def __str__(self):
#         addons_text = self.addons_included
#         if addons_text:
#             return f'Registration - {self.event.title} - Addons: {addons_text}'
        return f'Registration - {self.event.title}'

    @property
    def group(self):   
        return self.event.groups.first()

    @property
    def hash(self):
        return md5(".".join([str(self.event.pk), str(self.pk)]).encode()).hexdigest()

    @property
    def can_edit_child_events(self):
        """If any registrant can edit child events, return True"""
        if not self.event.nested_events_enabled:
            return False

        for registrant in self.registrant_set.filter(cancel_dt__isnull=True):
            if registrant.child_events.exists() and not registrant.registration_closed:
                return True
        return False

    def allow_adjust_invoice_by(self, request_user):
        """
        Returns whether or not the request_user can adjust invoice
        for this event registration.
        """
        if not request_user.is_anonymous:
            if request_user.is_superuser:
                return True
            # check if request_user is chapter leader or committee leader
            if get_setting('module', 'events', 'leadercanadjust'):
                [group] = self.event.groups.all()[:1] or [None]
                if group:
                    [committee] = group.committee_set.all()[:1] or [None]
                    if committee:
                        return committee.is_committee_leader(request_user)
    
                    [chapter] = group.chapter_set.all()[:1] or [None]
                    if chapter:
                        return chapter.is_chapter_leader(request_user)

        return False

    def payment_abandoned(self):
        if self.invoice and self.invoice.balance > 0 and \
            self.invoice.payment_set.filter(status_detail='').exists():
            # the payment was attempted a day ago but not finished - we can say
            # it is abandoned
            if self.invoice.create_dt + timedelta(days=1) < datetime.now():
                return True
        return False

    # Called by payments_pop_by_invoice_user in Payment model.
    def get_payment_description(self, inv):
        """
        The description will be sent to payment gateway and displayed on invoice.
        If not supplied, the default description will be generated.
        """
        description = 'Tendenci Invoice %d for Event (%d): %s - %s (RegId %d).' % (
            inv.id,
            self.event.pk,
            self.event.title,
            self.event.start_dt.strftime('%Y-%m-%d'),
            inv.object_id,
        )

        return _(description)

    def make_acct_entries(self, user, inv, amount, **kwargs):
        """
        Make the accounting entries for the event sale
        """
        from tendenci.apps.accountings.models import Acct, AcctEntry, AcctTran
        from tendenci.apps.accountings.utils import make_acct_entries_initial, make_acct_entries_closing

        ae = AcctEntry.objects.create_acct_entry(user, 'invoice', inv.id)
        if not inv.is_tendered:
            make_acct_entries_initial(user, ae, amount)
        else:
            # payment has now been received
            make_acct_entries_closing(user, ae, amount)

            # #CREDIT event SALES
            acct_number = self.get_acct_number()
            acct = Acct.objects.get(account_number=acct_number)
            AcctTran.objects.create_acct_tran(user, ae, acct, amount*(-1))

    # to lookup for the number, go to /accountings/account_numbers/
    def get_acct_number(self, discount=False):
        if discount:
            return 462000
        else:
            return 402000

    def auto_update_paid_object(self, request, payment):
        """
        Update the object after online payment is received.
        """
        try:
            from tendenci.apps.notifications import models as notification
        except:
            notification = None
        from tendenci.apps.events.utils import email_admins

        site_label = get_setting('site', 'global', 'sitedisplayname')
        site_url = get_setting('site', 'global', 'siteurl')
        self_reg8n = get_setting('module', 'users', 'selfregistration')

        payment_attempts = self.invoice.payment_set.count()

        registrants = self.registrant_set.all().order_by('id')
        for registrant in registrants:
            #registrant.assign_mapped_fields()
            if registrant.custom_reg_form_entry:
                registrant.name = str(registrant.custom_reg_form_entry)
            else:
                registrant.name = ' '.join([registrant.first_name, registrant.last_name])

        # only send email on success! or first fail
        if payment.is_paid or payment_attempts <= 1:
            if self.event.registration_configuration:
                reply_to = self.event.registration_configuration.reply_to
            else:
                reply_to = None
            notification.send_emails(
                [self.registrant.email],  # recipient(s)
                'event_registration_confirmation',  # template
                {
                    'SITE_GLOBAL_SITEDISPLAYNAME': site_label,
                    'SITE_GLOBAL_SITEURL': site_url,
                    'site_label': site_label,
                    'site_url': site_url,
                    'self_reg8n': self_reg8n,
                    'reg8n': self,
                    'registrants': registrants,
                    'event': self.event,
                    'total_amount': self.invoice.total,
                    'is_paid': payment.is_paid,
                    'reply_to': reply_to,
                },
                True,  # notice saved in db
            )
            #notify the admins too
            email_admins(self.event, self.invoice.total, self_reg8n, self, registrants)

    @property
    def allow_refunds(self):
        """Indicate if refunds are allowed"""
        return get_setting('module', 'events', 'allow_refunds') != "No"

    def refund(self, request, refund_amount, confirmation_message):
        """Refund this registration's invoice"""
        if not self.allow_refunds:
            return

        refund_amount = self.invoice.get_refund_amount(refund_amount)
        try:
            if refund_amount:
                self.invoice.refund(refund_amount, request.user, confirmation_message)
        except:
            messages.set_level(request, messages.ERROR)
            error_message = f"Refund in the amount of ${refund_amount} failed to process. " \
                            f"Please contact support."
            messages.error(request, _(error_message))

        messages.success(request, _(confirmation_message))

    def cancel(self, request, refund=True, cancellation_fees=None):
        """
        Cancel all registrants on this registration.
        Refunding here is optional in the case that it is done
        separately (ex in the Refund menu)

        Call registrant.cancel with check_registration_status set and
        refund set to False to  hold off on these tasks until after
        the loop.
        Set cancellation_fees if you need to override the default calcuated
        fees.
        """
        if self.canceled:
            return

        registrants = self.registrant_set.filter(cancel_dt__isnull=True)
        refund_amount = 0
        for registrant in registrants:
            registrant.cancel(
                request,
                check_registration_status=False,
                refund=False,
                process_cancellation_fee=cancellation_fees is None,
            )
            if registrant.amount:
                refund_amount += registrant.amount

        # Adjust and process cancellation fees if indicated
        if cancellation_fees is not None:
            self.process_adjusted_cancellation_fees(cancellation_fees, request.user)

        confirmation_message = self.event.get_refund_confirmation_message(registrants)

        # Refund if applicable
        if refund and self.invoice.can_auto_refund and refund_amount:
            self.refund(request, refund_amount, confirmation_message)

        self.canceled = True
        self.save()

    def process_adjusted_cancellation_fees(self, cancellation_fee, user=None):
        """
        Adjust and process cancellation fees for invoice.
        Set update_fee to True to update existing cancellation line item
        instead of adding a new one.
        """
        # Only applicable if refunds are enabled
        if not self.allow_refunds:
            return

        # Adjust cancellation_fee
        self.invoice.adjusted_cancellation_fees = cancellation_fee
        self.invoice.save(update_fields=['adjusted_cancellation_fees'])

        # Update invoice with adjusted cancellation fee
        self.invoice.update_cancellation_fee_line_item(cancellation_fee, user)

    def status(self):
        """
        Returns registration status.
        """
        config = self.event.registration_configuration

        balance = self.invoice.balance
        if self.reg_conf_price is None or self.reg_conf_price.payment_required is None:
            payment_required = config.payment_required
        else:
            payment_required = self.reg_conf_price.payment_required

        if self.canceled:
            return 'cancelled'

        if balance > 0:
            if payment_required:
                return 'payment-required'
            else:
                return 'registered-with-balance'
        else:
            return 'registered'

    @property
    def registrant(self):
        """
        Gets primary registrant.
        Get first registrant w/ email address
        Order by insertion (primary key)
        """
        [registrant] = self.registrant_set.filter(is_primary=True)[:1] or [None]
        if not registrant:
            [registrant] = self.registrant_set.all().order_by("pk")[:1] or [None]

        return registrant

    @property
    def graguity_in_percentage(self):
        return '{:.1%}'.format(self.gratuity)

    @property
    def default_cancellation_fees(self):
        """
        Default cancellation fee for registration is the
        sum of all fees for registrants.
        """
        fee = 0
        for registrant in self.registrant_set.all():
            fee += registrant.cancellation_fee

        return fee

    def save(self, *args, **kwargs):
        if not self.pk:
            self.guid = str(uuid.uuid4())
        super(Registration, self).save(*args, **kwargs)

    def get_invoice(self):
        object_type = ContentType.objects.get(app_label=self._meta.app_label,
            model=self._meta.model_name)

        try:
            invoice = Invoice.objects.get(
                object_type=object_type,
                object_id=self.pk,
            )
        except ObjectDoesNotExist:
            invoice = self.invoice

        return invoice

    def save_invoice(self, *args, **kwargs):
        status_detail = kwargs.get('status_detail', 'tendered')
        admin_notes = kwargs.get('admin_notes', None)

        object_type = ContentType.objects.get(app_label=self._meta.app_label,
            model=self._meta.model_name)

        try: # get invoice
            invoice = Invoice.objects.get(
                object_type = object_type,
                object_id = self.pk,
            )
        except ObjectDoesNotExist: # else; create invoice
            # cannot use get_or_create method
            # because too many fields are required
            invoice = Invoice()
            invoice.object_type = object_type
            invoice.object_id = self.pk

        # primary registrant is responsible for billing
        primary_registrant = self.registrant
        invoice.bill_to =  primary_registrant.first_name + ' ' + primary_registrant.last_name
        invoice.bill_to_first_name = primary_registrant.first_name
        invoice.bill_to_last_name = primary_registrant.last_name
        invoice.bill_to_company = primary_registrant.company_name
        invoice.bill_to_phone = primary_registrant.phone
        invoice.bill_to_email = primary_registrant.email
        invoice.bill_to_address = primary_registrant.address
        invoice.bill_to_city = primary_registrant.city
        invoice.bill_to_state = primary_registrant.state
        invoice.bill_to_zip_code = primary_registrant.zip
        invoice.bill_to_country =  primary_registrant.country
        invoice.ship_to = primary_registrant.first_name + ' ' + primary_registrant.last_name
        invoice.ship_to_first_name = primary_registrant.first_name
        invoice.ship_to_last_name = primary_registrant.last_name
        invoice.ship_to_company = primary_registrant.company_name
        invoice.ship_to_address = primary_registrant.address
        invoice.ship_to_city = primary_registrant.city
        invoice.ship_to_state = primary_registrant.state
        invoice.ship_to_zip_code =  primary_registrant.zip
        invoice.ship_to_country = primary_registrant.country
        invoice.ship_to_phone =  primary_registrant.phone
        invoice.ship_to_email = primary_registrant.email

        invoice.creator_id = self.creator_id
        invoice.owner_id = self.owner_id

        # update invoice with details
        invoice.title = "Registration %s for Event: %s" % (self.pk, self.event.title)
        invoice.estimate = ('estimate' == status_detail)
        invoice.status_detail = status_detail
        invoice.tender_date = datetime.now()
        invoice.due_date = datetime.now()
        invoice.ship_date = datetime.now()
        invoice.admin_notes = admin_notes
        invoice.gratuity = self.gratuity

        tax = 0
        if self.reg_conf_price and self.reg_conf_price.include_tax:
            tax = self.reg_conf_price.tax_rate * self.amount_paid
            invoice.tax = tax
        else:
            # generally non-table registration
            if self.registrant_set.filter(pricing__include_tax=True).exists():
                for override, override_price, price, tax_rate in self.registrant_set.filter(
                                pricing__include_tax=True).values_list(
                            'override', 'override_price',
                            'pricing__price', 'pricing__tax_rate'):
                    if override:
                        price = override_price
                    tax += price * tax_rate
                invoice.tax = tax

        invoice.subtotal = self.amount_paid
        invoice.total = invoice.subtotal + tax
            
        if invoice.gratuity:
            invoice.total += invoice.subtotal * invoice.gratuity
        invoice.balance = invoice.total
        invoice.save()

        self.invoice = invoice

        self.save()

        return invoice

    @property
    def has_overridden(self):
        if self.is_table:
            return self.override_table

        return self.registrant_set.filter(override=True).exists()

    @property
    def addons_included(self):
        addons_text = ''
        if not self.event.has_addons:
            return ''

        reg8n_to_addons_list = RegAddonOption.objects.filter(
            regaddon__registration=self).values_list(
                'regaddon__registration__id',
                'regaddon__addon__title',
                'option__title',
                'regaddon__amount')

        if reg8n_to_addons_list:
            currency_symbol = get_setting('site', 'global', 'currencysymbol')
            for addon_item in reg8n_to_addons_list:
                if addon_item[0] == self.registrant.registration_id:
                    addons_text += f'{addon_item[1]}({addon_item[2]})({currency_symbol}{addon_item[3]}) '

        return addons_text


class Registrant(models.Model):
    """
    Event registrant.
    An event can have multiple registrants.
    A registrant can go to multiple events.
    A registrant is static information.
    The names do not change nor does their information
    This is the information that was used while registering
    """
    registration = models.ForeignKey('Registration', on_delete=models.CASCADE)
    attendance_dates = models.JSONField(
        blank=True, null=True, help_text=_("The dates this registrant will be attending."))
    user = models.ForeignKey(User, blank=True, null=True, on_delete=models.SET_NULL)
    amount = models.DecimalField(_('Amount'), max_digits=21, decimal_places=2, blank=True, default=0)
    pricing = models.ForeignKey('RegConfPricing', null=True, on_delete=models.SET_NULL)  # used for dynamic pricing

    custom_reg_form_entry = models.ForeignKey(
        "CustomRegFormEntry", related_name="registrants", null=True, on_delete=models.CASCADE)

    name = models.CharField(max_length=100)
    salutation = models.CharField(_('salutation'), max_length=15,
                                  blank=True, default='')
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    mail_name = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    zip = models.CharField(max_length=50, blank=True)
    country = models.CharField(max_length=100, blank=True)

    phone = models.CharField(max_length=50, blank=True)
    email = models.CharField(max_length=100)
    groups = models.CharField(max_length=100)

    position_title = models.CharField(max_length=100, blank=True)
    company_name = models.CharField(max_length=100, blank=True)

    meal_option = models.CharField(max_length=200, default='', blank=True)
    comments = models.TextField(default='', blank=True)

    is_primary = models.BooleanField(_('Is primary registrant'), default=False)
    override = models.BooleanField(_('Admin Price Override?'), default=False)
    override_price = models.DecimalField(
        _('Override Price'), max_digits=21,
        decimal_places=2,
        blank=True,
        default=0
    )

    discount_amount = models.DecimalField(
        _('Discount Amount'),
        max_digits=10,
        decimal_places=2,
        default=0
    )
    certification_track = models.ForeignKey(Certification,
                                   null=True, blank=True,
                                   on_delete=models.SET_NULL)

    cancel_dt = models.DateTimeField(editable=False, null=True)
    memberid = models.CharField(_('Member ID'), max_length=50, blank=True, null=True)
    use_free_pass = models.BooleanField(default=False)

    checked_in = models.BooleanField(_('Is Checked In?'), default=False)
    checked_in_dt = models.DateTimeField(null=True)
    checked_out = models.BooleanField(_('Is Checked Out?'), default=False)
    checked_out_dt = models.DateTimeField(null=True)

    reminder = models.BooleanField(_('Receive event reminders'), default=False)

    create_dt = models.DateTimeField(auto_now_add=True)
    update_dt = models.DateTimeField(auto_now=True)

    objects = RegistrantManager()

    class Meta:
#         permissions = (("view_registrant", _("Can view registrant")),)
        app_label = 'events'

    def __str__(self):
        if self.custom_reg_form_entry:
            return self.custom_reg_form_entry.get_lastname_firstname()
        else:
            return '%s, %s' % (self.last_name, self.first_name)

    @property
    def registration_closed(self):
        """Returns whether registration is closed for this registrant's pricing"""
        return self.pricing.registration_has_ended

    @property
    def event(self):
        return self.registration.event

    @property
    def registration_configuration(self):
        return self.event.registration_configuration

    def register_pricing(self):
        # The pricing is a field recently added. The previous registrations
        # store the pricing in registration.
        return self.pricing or self.registration.reg_conf_price

    @property
    def upcoming_event_days(self):
        """Number of days upcoming covered by pricing"""
        if self.pricing and self.pricing.days_price_covers:
            return self.pricing.days_price_covers - len(self.past_attendance_dates)
        return 0

    @property
    def available_child_events(self):
        """Child events occurring on the dates Registrant is attending"""
        if not self.event.nested_events_enabled:
            return Event.objects.none()

        return self.event.child_events.filter(start_dt__date__in=self.attendance_dates)

    @property
    def past_attendance_dates(self):
        """Attendance dates for sessions in the past"""
        if self.attendance_dates:
            return [x for x in self.attendance_dates if parse(x).date() <= datetime.now().date()]
        return list()

    @property
    def upcoming_attendance_dates(self):
        """Attendance dates for future sessions"""
        if self.attendance_dates:
            return [x for x in self.attendance_dates if parse(x).date() > datetime.now().date()]
        return list()

    @property
    def can_edit_attendance_dates(self):
        """
        Attendance dates are editable if registration is not closed,
        nested events are enabled, event has child events
        """
        return (
            not self.registration_closed and
            self.event.nested_events_enabled and
            self.event.has_child_events
        )

    @property
    def sub_event_datetimes(self):
        """Returns list of start_dt for available sub events"""
        datetimes = dict()
        for event in self.available_child_events:
            if event.start_dt not in datetimes:
                datetimes[event.start_dt] = event.end_dt
            else:
                datetimes[event.start_dt] = max(datetimes[event.start_dt], event.end_dt)

        return datetimes

    def register_child_events(self, child_event_pks):
        """Register for child event"""
        # Remove any upcoming records that have been updated to 'not attending'
        self.registrantchildevent_set.filter(
            child_event__start_dt__date__gt=datetime.now().date()).exclude(
            child_event_id__in=child_event_pks,
        ).delete()

        # Add child event if it's not already registered
        for child_event_pk in child_event_pks:
            event = Event.objects.get(pk=child_event_pk)

            if self.registrantchildevent_set.filter(
                child_event__repeat_uuid=event.repeat_uuid
            ).exclude(child_event_id=event.pk).exists():

                current_event = self.registrantchildevent_set.filter(
                    child_event__repeat_uuid=event.repeat_uuid).first().child_event
                error = _(
                    f'{event.title} on {event.start_dt.date()} is a repeat of event on ' \
                    f'{current_event.start_dt.date()}. Please select only one.')
                raise Exception(error)

            RegistrantChildEvent.objects.get_or_create(
                child_event_id=child_event_pk,
                registrant_id=self.pk,
            )

    @property
    def child_events(self):
        """Child events registered Registrant is attending"""
        return self.registrantchildevent_set.all().order_by('child_event__start_dt')

    @property
    def child_events_available_for_check_in(self):
        """
        Child events available for check in
        These are child events not yet checked into, and
        that are upcoming today.
        """
        return self.child_events.filter(
            checked_in=False,
            child_event__start_dt__date=datetime.today()
        )

    @property
    def check_in_url(self):
        """URL to check registrant into event"""
        site_url = get_setting('site', 'global', 'siteurl')
        return f"{site_url}{reverse('event.digital_check_in', args=[self.pk])}"

    @property
    def cancellation_fee(self):
        """Cancellation fee for registrant"""
        return self.registration_configuration.get_cancellation_fee(self.amount)

    def process_cancellation_fee(self, user=None):
        """Add cancellation fee to invoice"""
        # Only applicable if refunds are enabled
        if not self.allow_refunds:
            return

        cancellation_fee = self.cancellation_fee

        if cancellation_fee:
            self.registration.invoice.add_line_item(
                cancellation_fee,
                Invoice.LineDescriptions.CANCELLATION_FEE,
                user,
                update_total=False,
            )

    @property
    def lastname_firstname(self):
        fn = self.first_name or None
        ln = self.last_name or None

        if fn and ln:
            return ', '.join([ln, fn])
        return fn or ln

    def check_in_or_out(self, check_in):
        """Check in or check out to/of main Event"""
        check_in_or_out = 'checked_in' if check_in else 'checked_out'
        datetime_field = 'checked_in_dt' if check_in else 'checked_out_dt'
        error_message_var = 'in' if check_in else 'out'
        error_message = \
            _(f'Registrant was not successfully checked {error_message_var}. Please try again')

        setattr(self, check_in_or_out, True)
        setattr(self, datetime_field, datetime.now())

        try:
            self.save(update_fields=[check_in_or_out, datetime_field])
        except:
            raise Exception(error_message)

    def get_name(self):
        if self.custom_reg_form_entry:
            return self.custom_reg_form_entry.get_name()
        else:
            if self.first_name or self.last_name:
                return self.first_name + ' ' + self.last_name

        if self.name:
            return self.name

        if not self.is_primary:
            [primary_registrant] = Registrant.objects.filter(
                                       is_primary=True,
                                       registration_id=self.registration.id
                                       ).values_list(
                                      'first_name', 'last_name')[:1] or [None]
            if primary_registrant:
                return _('Guest of {}').format(' '.join(primary_registrant))
        return None

    def course(self):
        event = self.registration.event
        return event.course

    @classmethod
    def event_registrants(cls, event=None):

        return cls.objects.filter(
            registration__event=event,
            cancel_dt=None,
        )

    @property
    def additional_registrants(self):
        # additional registrants on the same invoice
        return self.registration.registrant_set.filter(cancel_dt=None).exclude(id=self.id).order_by('id')

    @property
    def hash(self):
        return md5(".".join([str(self.registration.event.pk), str(self.pk)]).encode()).hexdigest()

    def hash_url(self):
        return reverse('event.registration_confirmation', args=[self.registration.event.pk, self.hash])

    def get_absolute_url(self):
        return reverse('event.registration_confirmation', args=[self.registration.event.pk, self.registration.pk])

    @property
    def invoice(self):
        """Invoice for this registrant"""
        return self.registration.invoice

    @property
    def check_out_enabled(self):
        """
        Check out is enabled if Event doesn't have child events or
        if nested events are not enabled.
        """
        return self.registration.event.check_out_enabled

    def cancel(self, request, check_registration_status=True, refund=True, process_cancellation_fee=True):
        """
        Cancel registrant.

        By default, check and update Registration status if all
        registrants have canceled. This can be turned off in the case
        of looping through and cancelling all registrants. In that
        case, it would be done at the end of the loop.
        See Registration.cancel

        By default, will refund if configured for auto refunds.
        Turn this off if refund is being done separately in Refund menu.
        Turn off process_cancellation_fee to bulk adjust cancellation fees
        for entire invoice separately.
        """
        if self.cancel_dt:
            return

        can_refund = False
        can_auto_refund = False
        self.cancel_dt = datetime.now()
        self.save()

        # update the amount_paid in registration
        if self.amount:
            if self.registration.amount_paid:
                self.registration.amount_paid -= self.amount
                self.registration.save()

            # update the invoice if invoice is not tendered
            if not self.invoice.is_tendered:
                self.invoice.total -= self.amount
                self.invoice.subtotal -= self.amount
                self.invoice.balance -= self.amount
                self.invoice.save(request.user)

            can_refund = self.invoice.can_refund
            can_auto_refund = self.invoice.can_auto_refund

            # Refund and apply cancellation fees if applicable
            if process_cancellation_fee:
                self.process_cancellation_fee(request.user)

            confirmation_message = None
            if self.invoice.can_auto_refund and self.amount:
                confirmation_message = self.event.get_refund_confirmation_message([self])

            if refund and can_auto_refund:
                self.refund(request, confirmation_message)

        # check if all registrants in this registration are canceled.
        # if so, update the canceled field.
        reg8n = self.registration
        if check_registration_status and not reg8n.registrant_set.filter(
                registration=reg8n,
                cancel_dt__isnull=True
        ).exists():
            reg8n.canceled = True
            reg8n.save()
        EventLog.objects.log(instance=self)

        # Notify of cancellation
        self.send_cancellation_notification(request.user, can_refund, can_auto_refund, refund)

    @property
    def allow_refunds(self):
        return get_setting('module', 'events', 'allow_refunds') != "No"

    def refund(self, request, confirmation_message):
        """Refund this registrant's invoice"""
        if not self.allow_refunds:
            return

        refund_amount = self.invoice.get_refund_amount(self.amount)
        try:
            if refund_amount:
                self.invoice.refund(refund_amount, request.user, confirmation_message)
        except:
            messages.set_level(request, messages.ERROR)
            error_message = f"Refund in the amount of ${refund_amount} failed to process. " \
                            f"Please contact support."
            messages.error(request, _(error_message))

        messages.success(request, _(confirmation_message))

    def send_cancellation_notification(self, user, can_refund, can_auto_refund, include_refund=True):
        """
        Send cancellation notification.

        include_refund defaults to True to include refund info in email.
        Turn this off if refund is separate. This can happen if refund is
        processed separately (ex: when cancelling from the Refund menu)
        """
        user_is_registrant = user.is_authenticated and self.user and user == self.user
        recipients = get_notice_recipients('site', 'global', 'allnoticerecipients')

        if recipients and notification:
            notification.send_emails(recipients, 'event_registration_cancelled', {
                'event': self.event,
                'user': user,
                'registrants_paid': self.event.registrants(with_balance=False),
                'registrants_pending': self.event.registrants(with_balance=True),
                'SITE_GLOBAL_SITEDISPLAYNAME': get_setting('site', 'global', 'sitedisplayname'),
                'SITE_GLOBAL_SITEURL': get_setting('site', 'global', 'siteurl'),
                'registrant': self,
                'user_is_registrant': user_is_registrant,
                'allow_refunds': self.allow_refunds and include_refund,
                'can_refund': can_refund and include_refund,
                'can_auto_refund': can_auto_refund and include_refund,
            })

    def reg8n_status(self):
        """
        Returns string status.
        """
        config = self.registration.event.registration_configuration

        invoice = self.registration.get_invoice()
        if invoice:
            balance = invoice.balance
        else:
            balance = 0

        if self.pricing is None or self.pricing.payment_required is None:
            payment_required = config.payment_required
        else:
            payment_required = self.pricing.payment_required

        if self.cancel_dt:
            return 'cancelled'

        if balance > 0:
            if payment_required:
                return 'payment-required'
            else:
                return 'registered-with-balance'
        else:
            return 'registered'

    def initialize_fields(self):
        """Similar to assign_mapped_fields but more direct and saves the registrant
        """
        if self.custom_reg_form_entry:
            self.first_name = self.custom_reg_form_entry.get_value_of_mapped_field('first_name')
            self.last_name = self.custom_reg_form_entry.get_value_of_mapped_field('last_name')
            self.mail_name = self.custom_reg_form_entry.get_value_of_mapped_field('mail_name')
            self.address = self.custom_reg_form_entry.get_value_of_mapped_field('address')
            self.city = self.custom_reg_form_entry.get_value_of_mapped_field('city')
            self.state = self.custom_reg_form_entry.get_value_of_mapped_field('state')
            self.zip = self.custom_reg_form_entry.get_value_of_mapped_field('zip')
            self.country = self.custom_reg_form_entry.get_value_of_mapped_field('country')
            self.phone = self.custom_reg_form_entry.get_value_of_mapped_field('phone')
            self.email = self.custom_reg_form_entry.get_value_of_mapped_field('email')
            self.groups = self.custom_reg_form_entry.get_value_of_mapped_field('groups')
            self.position_title = self.custom_reg_form_entry.get_value_of_mapped_field('position_title')
            self.company_name = self.custom_reg_form_entry.get_value_of_mapped_field('company_name')
        if self.first_name or self.last_name:
            self.name = ('%s %s' % (self.first_name, self.last_name)).strip()
        self.save()

    def assign_mapped_fields(self):
        """
        Assign the value of the mapped fields from custom registration form to this registrant
        """
        if self.custom_reg_form_entry:
            user_fields = [item[0] for item in USER_FIELD_CHOICES]
            for field in user_fields:
                setattr(self, 'field', self.custom_reg_form_entry.get_value_of_mapped_field(field))

            self.name = ('%s %s' % (self.first_name, self.last_name)).strip()

    def populate_custom_form_entry(self):
        """
        When, for some reason, registrants don't have the associated custom reg form entry
        registered for an event with a custom form, they cannot be edited.
        We're going to check and populate the entry if not existing so that they can edit.
        """
        if not self.custom_reg_form_entry:
            reg_conf = self.registration.event.registration_configuration
            if reg_conf.use_custom_reg_form:
                custom_reg_form = reg_conf.reg_form
                # add an entry for this registrant
                entry = CustomRegFormEntry.objects.create(entry_time=datetime.now(),
                                                  form=custom_reg_form)
                self.custom_reg_form_entry = entry
                self.save()
                # populate fields
                fields = [item[0] for item in USER_FIELD_CHOICES]
                for field_name in fields:
                    if hasattr(self, field_name):
                        value = getattr(self, field_name)
                        [field] = CustomRegField.objects.filter(
                                        form=custom_reg_form,
                                        map_to_field=field_name)[:1] or [None]
                        if field:
                            CustomRegFieldEntry.objects.create(
                                             value=value,
                                             entry=entry,
                                             field=field)


class RegistrantChildEvent(models.Model):
    child_event = models.ForeignKey('Event', on_delete=models.CASCADE)
    registrant = models.ForeignKey('Registrant', on_delete=models.CASCADE)
    checked_in = models.BooleanField(_('Is Checked In?'), default=False)
    checked_in_dt = models.DateTimeField(null=True)
    create_dt = models.DateTimeField(auto_now_add=True)
    update_dt = models.DateTimeField(auto_now=True)

    def __str__(self):
        event = self.child_event
        return f'{event.title} {event.start_dt.time().strftime("%I:%M %p")} - ' \
               f'{event.end_dt.time().strftime("%I:%M %p")}'


class Payment(models.Model):
    """
    Event registration payment
    Extends the registration model
    """
    registration = models.OneToOneField('Registration', on_delete=models.CASCADE)

    class Meta:
        app_label = 'events'


class PaymentMethod(models.Model):
    """
    This will hold available payment methods
    Default payment methods are 'Credit Card, Cash and Check.'
    Pre-populated via fixtures
    Soft Deletes required; For historical purposes.
    """
    label = models.CharField(max_length=50, blank=False)

    class Meta:
        app_label = 'events'

    def __str__(self):
        return self.label


class SponserLogo(File):
    class Meta:
        app_label = 'events'


class ImageUploader:
    def upload(self, file_obj, user, is_public, save=True):
        """Upload image"""
        image = self.upload_class()
        image.content_type = ContentType.objects.get_for_model(self.__class__)
        image.creator = user
        image.creator_username = user.username
        image.owner = user
        image.owner_username = user.username
        filename = "%s" % (file_obj.name)
        file_obj.file.seek(0)
        image.file.save(filename, file_obj)

        set_s3_file_permission(image.file, public=is_public)

        # By default, save image. Set save to false if you will be saving
        # the instance later.
        self.image = image
        if save:
            self.save(update_fields=['image'])


class Sponsor(ImageUploader, models.Model):
    """
    Event sponsor
    Event can have multiple sponsors
    Sponsor can contribute to multiple events
    """
    upload_class = SponserLogo

    event = models.ManyToManyField('Event')
    description = models.TextField(blank=True, default='')
    name = models.CharField(max_length=255, blank=True, null=True)
    image = models.ForeignKey(
        SponserLogo,
        help_text=_('Logo that represents organizer'),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        app_label = 'events'


class Discount(models.Model):
    """
    Event discount
    Event can have multiple discounts
    Discount can only be associated with one event
    """
    event = models.ForeignKey('Event', on_delete=models.CASCADE)
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)

    class Meta:
        app_label = 'events'


class OrganizerLogo(File):
    class Meta:
        app_label = 'events'


class Organizer(ImageUploader, models.Model):
    """
    Event organizer
    Event can have multiple organizers
    Organizer can maintain multiple events
    """
    upload_class = OrganizerLogo

    _original_name = None

    event = models.ManyToManyField('Event', blank=True)
    user = models.OneToOneField(User, blank=True, null=True, on_delete=models.CASCADE)
    name = models.CharField(max_length=100, blank=True) # static info.
    description = models.TextField(blank=True) # static info.
    image = models.ForeignKey(
        OrganizerLogo,
        help_text=_('Logo that represents organizer'),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        app_label = 'events'

    def __init__(self, *args, **kwargs):
        super(Organizer, self).__init__(*args, **kwargs)
        self._original_name = self.name

    def __str__(self):
        return self.name


class Speaker(OrderingBaseModel):
    """
    Event speaker
    Event can have multiple speakers
    Speaker can attend multiple events
    """
    _original_name = None

    event = models.ManyToManyField('Event', blank=True)
    user = models.OneToOneField(User, blank=True, null=True, on_delete=models.CASCADE)
    name = models.CharField(_('Speaker Name'), blank=True, max_length=100) # static info.
    description = models.TextField(blank=True) # static info.
    featured = models.BooleanField(
        default=False,
        help_text=_("All speakers marked as featured will be displayed when viewing the event."))

    class Meta:
        app_label = 'events'

    def __init__(self, *args, **kwargs):
        super(Speaker, self).__init__(*args, **kwargs)
        self._original_name = self.name

    def __str__(self):
        return self.name

    def files(self):
        return File.objects.get_for_model(self)

    def get_photo(self):

        if hasattr(self,'cached_photo'):
            return self.cached_photo

        files = File.objects.get_for_model(self).order_by('-update_dt')
        photos = [f for f in files if f.type() == 'image']

        photo = None
        if photos:
            photo = photos[0]  # most recent
            self.cached_photo = photo

        return photo


class RecurringEvent(models.Model):
    RECUR_DAILY = 1
    RECUR_WEEKLY = 2
    RECUR_MONTHLY = 3
    RECUR_YEARLY = 4
    RECURRENCE_CHOICES = (
        (RECUR_DAILY, _('Day(s)')),
        (RECUR_WEEKLY, _('Week(s)')),
        (RECUR_MONTHLY, _('Month(s)')),
        (RECUR_YEARLY, _('Year(s)'))
    )
    repeat_type = models.IntegerField(_("Repeats"), choices=RECURRENCE_CHOICES)
    frequency = models.IntegerField(_("Repeats every"))
    starts_on = models.DateTimeField()
    ends_on = models.DateTimeField()

    class Meta:
        verbose_name = _("Recurring Event")
        verbose_name_plural = _("Recurring Events")
        app_label = 'events'

    def get_info(self):
        if self.repeat_type == self.RECUR_DAILY:
            repeat_type = 'day(s)'
        elif self.repeat_type == self.RECUR_WEEKLY:
            repeat_type = 'week(s)'
        elif self.repeat_type == self.RECUR_MONTHLY:
            repeat_type = 'month(s)'
        elif self.repeat_type == self.RECUR_YEARLY:
            repeat_type = 'year(s)'
        ends_on = self.ends_on.strftime("%b %d %Y")
        return _("Repeats every %(frequency)s %(repeat_type)s until %(ends_on)s" % {
                            'frequency': self.frequency,
                            'repeat_type': repeat_type,
                            'ends_on': ends_on})


class EventPhoto(File):
    class Meta:
        app_label = 'events'


class Event(TendenciBaseModel):
    """
    Calendar Event
    """
    class EventRelationship:
        PARENT = 'parent'
        CHILD = 'child'

        CHOICES = (
            (PARENT, 'Is Parent Event'),
            (CHILD, 'Is Child Event')
        )

    guid = models.CharField(max_length=40, editable=False)
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        help_text=_("Larger symposium this event is a part of"),
    )
    event_relationship = models.CharField(
        max_length=50,
        choices=EventRelationship.CHOICES,
        default=EventRelationship.PARENT,
        help_text=_("Select 'child' if this is a sub-event of a larger symposium"),
    )
    type = models.ForeignKey(Type, blank=True, null=True, on_delete=models.SET_NULL)
    event_code = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text=_("Optional code representing this event.")
    )
    repeat_of = models.ForeignKey(
        'self',
        related_name="repeat_events",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_("Select if this child event is a repeat of another. "
                    "Registrants can only register for one instance of this child event.")
    )
    repeat_uuid = models.UUIDField(blank=True, null=True)
    title = models.CharField(max_length=150, blank=True)
    course = models.ForeignKey(Course, blank=True, null=True, on_delete=models.SET_NULL)
    short_name = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        help_text=_("Shorter name to display when space is limited (optional).")
    )
    delivery_method = models.CharField(max_length=150, blank=True, null=True)
    description = models.TextField(blank=True)
    all_day = models.BooleanField(default=False)
    start_dt = models.DateTimeField()
    end_dt = models.DateTimeField()
    timezone = TimeZoneField(verbose_name=_('Time Zone'), default='US/Central', choices=get_timezone_choices(), max_length=100)
    place = models.ForeignKey('Place', null=True, on_delete=models.SET_NULL)
    registration_configuration = models.OneToOneField('RegistrationConfiguration', null=True, editable=False, on_delete=models.CASCADE)
    mark_registration_ended = models.BooleanField(_('Registration Ended'), default=False)
    enable_private_slug = models.BooleanField(_('Enable Private URL'), blank=True, default=False) # hide from lists
    private_slug = models.CharField(max_length=500, blank=True, default=u'')
    password = models.CharField(max_length=50, blank=True)
    on_weekend = models.BooleanField(default=True, help_text=_("This event occurs on weekends"))
    external_url = models.URLField(_('External URL'), default=u'', blank=True)
    image = models.ForeignKey(EventPhoto,
        help_text=_('Photo that represents this event.'), null=True, blank=True, on_delete=models.SET_NULL)
    groups = models.ManyToManyField(Group, default=get_default_group, related_name='events')
    tags = TagField(blank=True)
    priority = models.BooleanField(default=False, help_text=_("Priority events will show up at the top of the event calendar day list and single day list. They will be featured with a star icon on the monthly calendar and the list view."))

    # recurring events
    is_recurring_event = models.BooleanField(_('Is Recurring Event'), default=False)
    recurring_event = models.ForeignKey(RecurringEvent, null=True, on_delete=models.CASCADE)

    # additional permissions
    display_event_registrants = models.BooleanField(_('Display Attendees'), default=False)
    DISPLAY_REGISTRANTS_TO_CHOICES=(("public",_("Everyone")),
                                    ("user",_("Users Only")),
                                    ("member",_("Members Only")),
                                    ("admin",_("Admin Only")),)
    display_registrants_to = models.CharField(max_length=6, choices=DISPLAY_REGISTRANTS_TO_CHOICES, default="admin")

    # html-meta tags
    meta = models.OneToOneField(MetaTags, null=True, on_delete=models.SET_NULL)

    perms = GenericRelation(ObjectPermission,
                                          object_id_field="object_id",
                                          content_type_field="content_type")


    objects = EventManager()

    class Meta:
#         permissions = (("view_event",_("Can view event")),)
        app_label = 'events'

    def __init__(self, *args, **kwargs):
        super(Event, self).__init__(*args, **kwargs)
        self.private_slug = self.private_slug or Event.make_slug()

    @property
    def has_child_events(self):
        """Indicate whether event has child events"""
        return self.nested_events_enabled and self.child_events.exists()

    @property
    def child_events(self):
        """All child events tied to this event"""
        return Event.objects.filter(parent_id=self.pk).order_by('start_dt')

    @property
    def can_configure_credits(self):
        """Indicates if credits can be configured on this Event"""
        return self.use_credits_enabled and not self.has_child_events

    @property
    def requires_attendance_dates(self):
        """
        Event registration requires attendance dates if nested events
        are enabled and this event has child events.
        """
        return self.nested_events_enabled and self.has_child_events

    @property
    def allow_credit_configuration_with_warning(self):
        """
        Indicates credit configuration allowed
        with warning that credits should be configured
        on the child event(s) if added.
        """
        return self.can_configure_credits and self.event_relationship == EventRelationship.PARENT

    def get_meta(self, name):
        """
        This method is standard across all models that are
        related to the Meta model.  Used to generate dynamic
        methods coupled to this instance.
        """
        return EventMeta().get_meta(self, name)

    def is_registrant(self, user):
        return Registration.objects.filter(event=self, registrant=user).exists()

    def is_registrant_user(self, user):
        if hasattr(user, 'registrant_set'):
            return user.registrant_set.filter(
                registration__event=self).exists()
        return False

    def get_absolute_url(self):
        return reverse('event', args=[self.pk])

    def get_absolute_edit_url(self):
        return reverse('event.edit', args=[self.pk])

    def get_registration_url(self):
        """ This is used to include a sign up url in the event.
        Sample usage in template:
        <a href="{{ event.get_registration_url }}">Sign up now!</a>
        """
        return reverse('registration_event_register', args=[self.pk])

    def save(self, *args, **kwargs):
        self.repeat_uuid = self.repeat_uuid or uuid.uuid4()
        self.guid = self.guid or str(uuid.uuid4())
        super(Event, self).save(*args, **kwargs)

        if self.image:
            set_s3_file_permission(self.image.file, public=self.is_public())

    def __str__(self):
        return f'{self.title} ({self.start_dt.strftime("%m/%d/%Y")} - {self.end_dt.strftime("%m/%d/%Y")})'

    @property
    def has_addons(self):
        return Addon.objects.filter(
            event=self,
            status=True
            ).exists()

    @property
    def nested_events_enabled(self):
        """Indicates if nested_events is enabled"""
        return get_setting("module", "events", "nested_events")

    @property
    def use_credits_enabled(self):
        """Indicates if use_credits is enabled"""
        return get_setting("module", "events", "use_credits")

    @cached_property
    def credits(self):
        """Credits configured for this Event"""
        return self.eventcredit_set.all()

    def get_or_create_credit_configuration(self, ceu_category_id, should_create):
        """Get or create credit configuration for a given CEUCategory"""
        category = CEUCategory.objects.get(pk=ceu_category_id)
        credit = self.get_credit_configuration(category)

        if not credit and should_create:
            credit = EventCredit.objects.create(ceu_subcategory_id=ceu_category_id)
            credit.event.add(self)

        return credit

    def get_credit_configuration(self, ceu_category):
        """Get credit configuration for given CEUCategory"""
        return self.credits.filter(ceu_subcategory=ceu_category).first()

    # this function is to display the event date in a nice way.
    # example format: Thursday, August 12, 2010 8:30 AM - 05:30 PM - GJQ 8/12/2010
    def dt_display(self, format_date='%a, %b %d, %Y', format_time='%I:%M %p'):
        return format_datetime_range(self.start_dt, self.end_dt, format_date, format_time)

    @property
    def check_out_enabled(self):
        """
        Check out is enabled if Event doesn't have child events and
        isn't a child event or if nested events are not enabled.
        """
        return (
            not self.nested_events_enabled or
            (not self.has_child_events and not self.parent)
        )

    @property
    def can_cancel(self):
        """
        Indicate whether cancelleation is allowed.
        """
        cancel_by_dt = self.registration_configuration.cancel_by_dt
        return not cancel_by_dt or cancel_by_dt + timedelta(days=1) >= datetime.now()

    @property
    def is_over(self):
        return self.end_dt <= datetime.now()

    @property
    def money_collected(self):
        """
        Total collected from this event
        """
        total_sum = Registration.objects.filter(event=self, canceled=False).aggregate(
            Sum('invoice__total')
        )['invoice__total__sum']

        # total_sum is the amount of money received when all is said and done
        if total_sum:
            return total_sum - self.money_outstanding
        return 0

    @property
    def money_outstanding(self):
        """
        Outstanding balance for this event
        """
        balance_sum = Registration.objects.filter(event=self, canceled=False).aggregate(
            Sum('invoice__balance')
        )['invoice__balance__sum']
        return balance_sum or 0

    @property
    def money_total(self):
        return self.money_collected + self.money_outstanding

    @property
    def registration_total(self):
        if not self.has_addons:
            return self.money_total
        else:
            return self.money_total - self.addons_total

    @property
    def addons_total(self):
        total_addons = 0
        if self.has_addons:
            registrations = Registration.objects.filter(event=self, canceled=False)
            for reg in registrations:
                total_addons += reg.regaddon_set.all().aggregate(Sum('amount'))['amount__sum'] or 0
        return total_addons

    @property
    def discount_count(self):
        """
        Count the number of discount codes used for this event.
        """
        return Registration.objects.filter(event=self, canceled=False,
                                           invoice__discount_amount__gt=0).count()
    
    @property
    def date(self):
        if self.start_dt and self.end_dt:
            if self.start_dt.year == self.end_dt.year:
                if self.start_dt.month == self.end_dt.month:
                    if self.start_dt.day == self.end_dt.day:
                        return "{0.day} {0:%b %Y}".format(self.start_dt)
                    else:
                        return "{0.day} - {1.day} {1:%b %Y}".format(self.start_dt, self.end_dt)
                else:
                    return "{0.day}{0:%b} - {1.day} {1:%b %Y}".format(self.start_dt, self.end_dt)
            else:
                return "{0.day}{0:%b %Y} - {1.day} {1:%b %Y}".format(self.start_dt, self.end_dt)
        else:
            if self.start_dt:
                return "{0.day} {0:%b %Y}".format(self.start_dt)
        return ''

    def get_cancellation_confirmation_message(self, registrants):
        """
        Get cancellation confirmation message for registrants.
        Message will vary based on allow_refunds setting.
        """
        allow_refunds = get_setting("module", "events", "allow_refunds")

        message = None

        if allow_refunds == "Yes":
            message = f"You have canceled your registration to { self.title } on " \
                      f"{ self.display_start_date }. You will receive an email confirmation " \
                      f"with a link to your updated invoice once event administrators " \
                      f"have processed your refund."
        elif allow_refunds == "Auto":
            message = self.get_refund_confirmation_message(registrants, True)

        return message

    def get_refund_confirmation_message(self, registrants, include_invoice_url=False):
        """
        Get refund confirmation message for registrants.
        In some cases, we will leave out the invoice_url (ex. in an email where we already include it).
        This will also be the cancellation confirmation message when auto refunds are turned on.
        """
        invoice = registrants[0].invoice
        amount = 0
        fee = 0

        for registrant in registrants:
            amount += registrant.amount
            fee += registrant.cancellation_fee

        amount = invoice.get_refund_amount(amount)

        cancellation_fee_message = ""
        if invoice.pending_cancellation_fees and amount > invoice.pending_cancellation_fees:
            cancellation_fee_message = f", and your cancellation fee of ${invoice.pending_cancellation_fees} processed"

        message = f"Your registration fee in the amount of ${amount} for {self.title} on " \
                  f"{self.display_start_date} has been canceled{cancellation_fee_message}. "

        if include_invoice_url:
            invoice_url = reverse('invoice.view', args=[invoice.pk, invoice.guid])
            message += f"You may access your final registration invoice " \
                       f"<a class='alert-link' href={invoice_url}> here</a>."

        return message

    def registrants(self, **kwargs):
        """
        This method can return 3 different values.
        All registrants, registrants with a balance, registrants without a balance.
        This method does not respect permissions.
        """

        registrants = Registrant.objects.filter(registration__event=self, cancel_dt=None)

        if 'with_balance' in kwargs:
            with_balance = kwargs['with_balance']

            if with_balance:
                registrants = registrants.filter(registration__invoice__balance__gt=0)
            else:
                registrants = registrants.filter(registration__invoice__balance__lte=0)

        return registrants

    def registrants_count(self, **kwargs):
        return self.registrants(**kwargs).count()

    def can_view_registrants(self, user):
        if self.display_event_registrants:
            if self.display_registrants_to == 'public':
                return True
            if user.profile.is_superuser and self.display_registrants_to == 'admin':
                return True
            if user.profile.is_member and self.display_registrants_to == 'member':
                return True
            if not user.is_anonymous and self.display_registrants_to == 'user':
                return True

        return False

    def speakers(self, **kwargs):
        """
        This method can returns the list of speakers associated with an event.
        Speakers with no name are excluded in the list.
        """

        speakers = self.speaker_set.exclude(name="").order_by('pk')

        return speakers

    def number_of_days(self):
        delta = self.end_dt - self.start_dt
        return delta.days

    @property
    def photo(self):
        if self.image:
            return self.image.file
        return None

    @property
    def display_start_date(self):
        """Start date formatted for confirmation messages"""
        return self.start_dt.strftime("%m/%d/%Y")

    def date_range(self, start_date, end_date):
        for n in range((end_date - start_date).days):
            yield start_date + timedelta(n)

    def date_spans(self):
        """
        Returns a list of date spans.
        e.g. s['start_dt'], s['end_dt'], s['same_date']
        """

        if self.on_weekend:
            same_date = self.start_dt.date() == self.end_dt.date()
            yield {'start_dt':self.start_dt, 'end_dt':self.end_dt, 'same_date':same_date}
            return

        start_dt = self.start_dt
        end_dt = None

        for date in self.date_range(self.start_dt, self.end_dt + timedelta(days=1)):

            if date.weekday() == 0:  # monday
                start_dt = date
            elif date.weekday() == 4:  # friday
                end_dt = date.replace(hour=self.end_dt.hour, minute=self.end_dt.minute, second=self.end_dt.second)

            if start_dt and end_dt:
                same_date = start_dt.date() == end_dt.date()
                yield {'start_dt':start_dt, 'end_dt':end_dt, 'same_date':same_date}
                start_dt = end_dt = None  # reset

        if start_dt and not end_dt:
            same_date = start_dt.date() == self.end_dt.date()
            yield {'start_dt':start_dt, 'end_dt':self.end_dt, 'same_date':same_date}

    @property
    def days(self):
        """
        List of each day of event covered by a sub-event.
        This is used to provide a list of potential attendance dates
        to filter sub-events by date. Includes dates for upcoming sessions only.
        """
        days = set()

        for event in self.child_events.filter(start_dt__date__gt=datetime.now()):
            spans = event.date_spans()

            for span in spans:
                if span['same_date']:
                    days.add(datetime.date(span['start_dt']))
                else:
                    date_range = self.date_range(
                        span['start_dt'], span['end_dt'] + timedelta(days=1))
                    date_range = [datetime.date(x) for x in date_range]
                    days.update(date_range)

        return sorted(days)

    def get_spots_status(self):
        """
        Return a tuple of (spots_taken, spots_available) for this event.
        """
        limit = self.get_limit()
        payment_required = self.registration_configuration.payment_required

        params = {
            'registration__event': self,
            'cancel_dt__isnull': True
        }

        if payment_required:
            params['registration__invoice__balance'] = 0

        spots_taken = Registrant.objects.filter(**params).count()

        if limit == 0:  # no limit
            return (spots_taken, -1)

        if spots_taken >= limit:
            return (spots_taken, 0)

        return (spots_taken, limit-spots_taken)

    def is_public(self):
        return all([self.allow_anonymous_view,
                self.status,
                self.status_detail in ['active']])

    def get_limit(self):
        """
        Return the limit for registration if it exists.
        """
        limit = 0
        if self.registration_configuration:
            limit = self.registration_configuration.limit
        return int(limit)

    @property
    def total_registered(self):
        """Total registered for this Event"""
        # If this is a child event, registration information is in RegistrationChildEvent
        if self.parent and self.nested_events_enabled:
            return RegistrantChildEvent.objects.filter(
                child_event_id=self.pk, registrant__cancel_dt__isnull=True).count()

        return self.registrants_count({'cancel_dt__isnull': True})

    @property
    def at_capacity(self):
        """Indicates if Event is at capacity"""
        limit = self.get_limit()
        if not limit:
            return False

        return self.total_registered >= limit

    @classmethod
    def make_slug(self, length=7):
        """
        Returns newly generated slug
        Option: length (default: 7)
        """
        return uuid.uuid4().hex[:length]

    def get_private_slug(self, absolute_url=False):
        """
        Returns private slug
        Option to return absolute private URL
        """
        from tendenci.apps.site_settings.utils import (
            get_module_setting,
            get_global_setting)

        pk = self.pk or 'id'
        private_slug = self.private_slug or Event.make_slug()

        if absolute_url:
            return '%s/%s/%s/%s' % (
                get_global_setting('siteurl'),
                get_module_setting('events', 'url') or 'events',
                pk,
                private_slug)

        self.private_slug = private_slug
        return private_slug

    def is_private(self, slug=u''):
        """
        Check if event is private (i.e. if private enabled)
        """
        # print('private_slug', self.private_slug)
        # print('slug', slug)

        return all((self.enable_private_slug, self.private_slug, self.private_slug == slug))

    def get_certification_choices(self):
        choices = []
        if self.course:
            school_category = self.course.school_category
            if school_category:
                choices.append(('', '----------'))
                for certcat in school_category.certcat_set.all():
                    choices.append((certcat.certification.id, certcat.certification.name))
        return choices

    def reg_start_dt(self):
        """
        Registration start date.
        """
        [pricing] = RegConfPricing.objects.filter(
                    reg_conf=self.registration_configuration,
                    status=True).order_by('start_dt')[:1] or [None]
        if pricing:
            return pricing.start_dt

    def reg_end_dt(self):
        """
        Registration end date.
        """
        [pricing] = RegConfPricing.objects.filter(
                    reg_conf=self.registration_configuration,
                    status=True).order_by('-end_dt')[:1] or [None]
        if pricing:
            return pricing.end_dt


class StandardRegForm(models.Model):
    """
    Dummy model to enable us of having an admin options in the
    Events section to edit the Standard Registration Form
    """
    class Meta:
        managed = False
        verbose_name = _("Standard Registration Form")
        verbose_name_plural = _("Standard Registration Form")
        app_label = 'events'


class CustomRegForm(models.Model):
    name = models.CharField(_("Name"), max_length=50)
    notes = models.TextField(_("Notes"), max_length=2000, blank=True)

    create_dt = models.DateTimeField(auto_now_add=True)
    update_dt = models.DateTimeField(auto_now=True)
    creator = models.ForeignKey(User, related_name="custom_reg_creator", null=True, on_delete=models.SET_NULL)
    creator_username = models.CharField(max_length=150)
    owner = models.ForeignKey(User, related_name="custom_reg_owner", null=True, on_delete=models.SET_NULL)
    owner_username = models.CharField(max_length=150)
    status = models.CharField(max_length=50, default='active')

    # registrant fields to be selected
    first_name = models.BooleanField(_('First Name'), default=False)
    last_name = models.BooleanField(_('Last Name'), default=False)
    mail_name = models.BooleanField(_('Mail Name'), default=False)
    address = models.BooleanField(_('Address'), default=False)
    city = models.BooleanField(_('City'), default=False)
    state = models.BooleanField(_('State'), default=False)
    zip = models.BooleanField(_('Zip'), default=False)
    country = models.BooleanField(_('Country'), default=False)
    phone = models.BooleanField(_('Phone'), default=False)
    email = models.BooleanField(_('Email'), default=False)
    position_title = models.BooleanField(_('Position Title'), default=False)
    company_name = models.BooleanField(_('Company'), default=False)
    meal_option = models.BooleanField(_('Meal Option'), default=False)
    comments = models.BooleanField(_('Comments'), default=False)

    class Meta:
        verbose_name = _("Custom Registration Form")
        verbose_name_plural = _("Custom Registration Forms")
        app_label = 'events'

    def __str__(self):
        return self.name

    @property
    def is_template(self):
        """
        A custom registration form is a template when it is not associated with
        registration configuration and any event registration conf pricing.
        A form template can be re-used and will be cloned if it is selected by
        a regconf or an regconfpricing.
        """
        if self.regconfs.exists() or self.regconfpricings.exists():
            return False
        return True

    def clone(self):
        """
        Clone this custom registration form and associate it with the event if provided.
        """
        params = dict([(field.name, getattr(self, field.name))
                       for field in self._meta.fields if not field.__class__==AutoField])
        cloned_obj = self.__class__.objects.create(**params)
        # clone fiellds
        fields = self.fields.all()
        for field in fields:
            field.clone(form=cloned_obj)

        return cloned_obj

    @property
    def has_regconf(self):
        return self.regconfs.all().exists() or None

    @property
    def for_event(self):
        event = None
        regconf = self.regconfs.all()[:1]
        if regconf:
            event = regconf[0].event
            return event.title

        return ''


class CustomRegField(OrderingBaseModel):
    form = models.ForeignKey("CustomRegForm", related_name="fields", on_delete=models.CASCADE)
    label = models.CharField(_("Label"), max_length=LABEL_MAX_LENGTH)
    map_to_field = models.CharField(_("Map to User Field"), choices=USER_FIELD_CHOICES,
        max_length=64, blank=True, null=True)
    field_type = models.CharField(_("Type"), choices=FIELD_TYPE_CHOICES,
        max_length=64)
    field_function = models.CharField(_("Special Functionality"),
        choices=FIELD_FUNCTIONS, max_length=64, null=True, blank=True)
    required = models.BooleanField(_("Required"), default=True)
    visible = models.BooleanField(_("Visible"), default=True)
    choices = models.CharField(_("Choices"), max_length=1000, blank=True,
        help_text=_("Comma separated options where applicable"))
    default = models.CharField(_("Default"), max_length=1000, blank=True,
        help_text=_("Default value of the field"))
    display_on_roster = models.BooleanField(_("Show on Roster"), default=False)

    class Meta:
        verbose_name = _("Field")
        verbose_name_plural = _("Fields")
        ordering = ('position',)
        app_label = 'events'

    def clone(self, form=None):
        """
        Clone this custom registration field, and associate it with the form if provided.
        """
        params = dict([(field.name, getattr(self, field.name))
                       for field in self._meta.fields if not field.__class__==AutoField])
        cloned_field = self.__class__.objects.create(**params)

        if form:
            cloned_field.form = form
            cloned_field.save()
        return cloned_field

    def execute_function(self, entry, value, user=None):
        if self.field_function == "GroupSubscription":
            if value:
                for val in self.choices.split(','):
                    group = Group.objects.get(name=val.strip())
                    if user:
                        try:
                            group_membership = GroupMembership.objects.get(group=group, member=user)
                        except GroupMembership.DoesNotExist:
                            group_membership = GroupMembership(group=group, member=user)
                            group_membership.creator_id = user.id
                            group_membership.creator_username = user.username
                            group_membership.role = 'subscriber'
                            group_membership.owner_id = user.id
                            group_membership.owner_username = user.username
                            group_membership.save()


class CustomRegFormEntry(models.Model):
    form = models.ForeignKey("CustomRegForm", related_name="entries", on_delete=models.CASCADE)
    entry_time = models.DateTimeField(_("Date/time"))

    class Meta:
        app_label = 'events'

    def __str__(self):
        name = self.get_name()
        if name:
            return name

        # top 2 fields
        values = []
        top_fields = CustomRegField.objects.filter(form=self.form,
                                                   field_type='CharField'
                                                   ).order_by('position')[0:2]
        for field in top_fields:
            field_entries = field.entries.filter(entry=self)
            if field_entries:
                values.append(field_entries[0].value)
        return (' '.join(values)).strip()

    def get_value_of_mapped_field(self, map_to_field):
        mapped_field = CustomRegField.objects.filter(form=self.form,
                                map_to_field=map_to_field)
        if mapped_field:
            #field_entries = CustomRegFieldEntry.objects.filter(entry=self, field=mapped_field[0])
            field_entries = mapped_field[0].entries.filter(entry=self)
            if field_entries:
                return (field_entries[0].value).strip()
        return ''

    def get_name(self):
        first_name = self.get_value_of_mapped_field('first_name')
        last_name = self.get_value_of_mapped_field('last_name')
        if first_name or last_name:
            name = ' '.join([first_name, last_name])
            return name.strip()
        return ''

    def get_lastname_firstname(self):
        name = '%s, %s' % (self.get_value_of_mapped_field('last_name'),
                         self.get_value_of_mapped_field('first_name'))
        return name.strip()

    def get_email(self):
        return self.get_value_of_mapped_field('email')

    def get_field_entry_list(self):
        field_entries = self.field_entries.order_by('field')
        entry_list = []
        for field_entry in field_entries:
            entry_list.append({'label': field_entry.field.label, 'value': field_entry.value})
        return entry_list

    def get_non_mapped_field_entry_list(self):
        field_entries = self.field_entries
        mapped_fields = [item[0] for item in USER_FIELD_CHOICES]
        field_entries = field_entries.exclude(field__map_to_field__in=mapped_fields).order_by('field')
        entry_list = []
        for field_entry in field_entries:
            entry_list.append({'label': field_entry.field.label, 'value': field_entry.value})
        return entry_list

    def roster_field_entry_list(self):
        list_on_roster = []
        field_entries = self.field_entries.exclude(field__map_to_field__in=[
                                    'first_name',
                                    'last_name',
                                    'position_title',
                                    'company_name'
                                    ]).filter(field__display_on_roster=1).order_by('field')

        for field_entry in field_entries:
            list_on_roster.append({'label': field_entry.field.label, 'value': field_entry.value})
        return list_on_roster

    def set_group_subscribers(self, user):
        for entry in self.field_entries.filter(field__field_function="GroupSubscription"):
            entry.field.execute_function(self, entry.value, user=user)

    def get_certification_track(self):
        [registrant] = Registrant.objects.filter(custom_reg_form_entry=self)[:1] or None
        if registrant and registrant.certification_track:
            return registrant.certification_track.id

        return None


class CustomRegFieldEntry(models.Model):
    entry = models.ForeignKey("CustomRegFormEntry", related_name="field_entries", on_delete=models.CASCADE)
    field = models.ForeignKey("CustomRegField", related_name="entries", on_delete=models.CASCADE)
    value = models.CharField(max_length=FIELD_MAX_LENGTH)

    class Meta:
        app_label = 'events'

class Addon(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)

    class Meta:
        app_label = 'events'

    price = models.DecimalField(_('Price'), max_digits=21, decimal_places=2, default=0)
    # permission fields
    group = models.ForeignKey(Group, blank=True, null=True, on_delete=models.SET_NULL)
    default_yes = models.BooleanField(_("Default to yes"), default=False,
                    help_text=_('Default the Add-on to yes so the registrant has to purposefully opt-out'))
    allow_anonymous = models.BooleanField(_("Public can use"), default=False)
    allow_user = models.BooleanField(_("Signed in user can use"), default=False)
    allow_member = models.BooleanField(_("All members can use"), default=False)

    status = models.BooleanField(default=True)

    def delete(self, from_db=False, *args, **kwargs):
        """
        Note that the delete() method for an object is not necessarily
        called when deleting objects in bulk using a QuerySet.
        """
        if not from_db:
            # set status to False (AKA Disable only)
            self.status = False
            self.save(*args, **kwargs)
        else:
            # actual delete of an Addon
            super(Addon, self).delete(*args, **kwargs)

    def __str__(self):
        return self.title

    def available(self):
        if not self.reg_conf.enabled or not self.status:
            return False
        if hasattr(self, 'event'):
            if datetime.now() > self.event.end_dt:
                return False
        return True

    def field_name(self):
        return "%s_%s" % (self.pk, self.title.lower().replace(' ', '').replace('-', ''))


class AddonOption(models.Model):
    addon = models.ForeignKey(Addon, related_name="options", on_delete=models.CASCADE)
    title = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text='If only one option, please use the first "Title" field ' \
                  'at the top of the form instead. Radio buttons will not ' \
                  'be displayed if more than one option is not offered.'
    )
    # old field for 2 level options (e.g. Option: Size -> Choices: small, large)
    # choices = models.CharField(max_length=200, help_text=_('options are separated by commas, ex: option 1, option 2, option 3'))

    class Meta:
        app_label = 'events'

    def __str__(self):
        return self.title


class RegAddon(models.Model):
    """Event registration addon.
    An event registration can avail multiple addons.
    This stores the addon's price at the time of registration.
    This stores the user's selected options for the addon.
    """
    registration = models.ForeignKey('Registration', on_delete=models.CASCADE)
    addon = models.ForeignKey('Addon', on_delete=models.CASCADE)

    # price at the moment of registration
    amount = models.DecimalField(_('Amount'), max_digits=21, decimal_places=2, default=0)

    create_dt = models.DateTimeField(auto_now_add=True)
    update_dt = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'events'

    def __str__(self):
        return "%s: %s" % (self.registration.pk, self.addon.title)


class RegAddonOption(models.Model):
    """Selected event registration addon option.
    """
    regaddon = models.ForeignKey(RegAddon, on_delete=models.CASCADE)
    option = models.ForeignKey(AddonOption, on_delete=models.CASCADE)
    # old field for 2 level options (e.g. Option: Size -> Choices: small, large)
    # selected_option = models.CharField(max_length=50)

    class Meta:
        unique_together = (('regaddon', 'option'),)
        app_label = 'events'

    def __str__(self):
        #return "%s: %s - %s" % (self.regaddon.pk, self.option.title, self.selected_option)
        return "%s: %s" % (self.regaddon.pk, self.option.title)
