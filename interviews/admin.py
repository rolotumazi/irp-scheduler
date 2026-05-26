from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    Interview,
    InterviewPanelist,
    Notification,
    PanelistAvailability,
    PreferenceRound,
    ProposalAssignment,
    RescheduleRequest,
    SchedulePublication,
    Timeslot,
    User,
)
from .notifications import enqueue_preference_request
from .solver import propose, publish


@admin.action(description='Queue preference-request emails to panelists')
def queue_preference_requests(modeladmin, request, queryset):
    interviewers = list(User.objects.filter(role=User.Role.INTERVIEWER, is_active=True))
    total = 0
    for round_obj in queryset:
        for user in interviewers:
            _, created = enqueue_preference_request(round_obj, user)
            total += created
    modeladmin.message_user(request, f'{total} preference-request email(s) queued.')


@admin.action(description='Propose a schedule (run the solver)')
def propose_schedule(modeladmin, request, queryset):
    for round_obj in queryset:
        pub = propose(round_obj, created_by=request.user)
        placed = pub.assignments.exclude(timeslot=None).count()
        unplaced = pub.assignments.filter(timeslot=None).count()
        modeladmin.message_user(
            request,
            f'Proposal #{pub.pk} for "{round_obj.name}": '
            f'{placed} placed, {unplaced} unplaceable (solver: {pub.solver_status}).',
        )


@admin.action(description='Publish proposal (apply timeslots + email participants)')
def publish_proposal(modeladmin, request, queryset):
    for pub in queryset:
        if pub.status != SchedulePublication.Status.PROPOSED:
            modeladmin.message_user(
                request, f'Skipped #{pub.pk}: already {pub.status}.', level='warning',
            )
            continue
        changed = publish(pub)
        modeladmin.message_user(
            request, f'Published #{pub.pk}: {changed} interview(s) changed slot.',
        )


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ('email', 'username', 'first_name', 'last_name', 'role', 'is_staff')
    list_filter = ('role', 'is_staff', 'is_superuser', 'is_active')
    search_fields = ('email', 'username', 'first_name', 'last_name')
    ordering = ('email',)
    fieldsets = DjangoUserAdmin.fieldsets + (
        ('Role', {'fields': ('role',)}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ('Role', {'fields': ('role',)}),
    )


@admin.register(Timeslot)
class TimeslotAdmin(admin.ModelAdmin):
    list_display = ('slot_ref', 'day_label', 'start_at', 'end_at', 'room_label', 'has_link')
    list_filter = ('day_label', 'room_label')
    search_fields = ('slot_ref', 'day_label', 'room_label')
    ordering = ('start_at', 'room_label')

    @admin.display(boolean=True, description='Link')
    def has_link(self, obj):
        return bool(obj.teams_link)


class InterviewPanelistInline(admin.TabularInline):
    model = InterviewPanelist
    extra = 1
    autocomplete_fields = ('user',)


@admin.register(Interview)
class InterviewAdmin(admin.ModelAdmin):
    list_display = ('external_id', 'title', 'timeslot', 'interviewee', 'status')
    list_filter = ('status', 'timeslot__day_label')
    search_fields = ('external_id', 'title', 'interviewee__email', 'interviewee__last_name')
    autocomplete_fields = ('timeslot', 'interviewee')
    inlines = [InterviewPanelistInline]


@admin.register(PreferenceRound)
class PreferenceRoundAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'opens_at', 'closes_at', 'min_available_slots')
    list_filter = ('status',)
    search_fields = ('name',)
    actions = [queue_preference_requests, propose_schedule]


class ProposalAssignmentInline(admin.TabularInline):
    model = ProposalAssignment
    extra = 0
    fields = ('interview', 'timeslot', 'reason')
    readonly_fields = ('interview', 'timeslot', 'reason')
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SchedulePublication)
class SchedulePublicationAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'round', 'status', 'solver_status', 'placed_count',
        'unplaced_count', 'created_at', 'published_at',
    )
    list_filter = ('status', 'round')
    readonly_fields = (
        'round', 'created_by', 'solver_status', 'objective_value',
        'created_at', 'published_at',
    )
    actions = [publish_proposal]
    inlines = [ProposalAssignmentInline]

    @admin.display(description='Placed')
    def placed_count(self, obj):
        return obj.assignments.exclude(timeslot=None).count()

    @admin.display(description='Unplaced')
    def unplaced_count(self, obj):
        return obj.assignments.filter(timeslot=None).count()


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('kind', 'recipient', 'status', 'created_at', 'sent_at')
    list_filter = ('kind', 'status')
    search_fields = ('recipient__email', 'subject', 'dedupe_key')
    readonly_fields = ('created_at', 'sent_at')


@admin.register(PanelistAvailability)
class PanelistAvailabilityAdmin(admin.ModelAdmin):
    list_display = ('round', 'panelist', 'timeslot', 'state')
    list_filter = ('round', 'state')
    search_fields = ('panelist__email', 'timeslot__slot_ref')
    autocomplete_fields = ('round', 'panelist', 'timeslot')


@admin.register(RescheduleRequest)
class RescheduleRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'interview', 'requested_by', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('interview__external_id', 'requested_by__email', 'external_ref')
    autocomplete_fields = ('interview', 'requested_by')
    readonly_fields = ('created_at', 'updated_at')
