from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    Interview,
    InterviewPanelist,
    PanelistAvailability,
    PreferenceRound,
    RescheduleRequest,
    Timeslot,
    User,
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
