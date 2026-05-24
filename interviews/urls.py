from django.urls import path
from sesame.views import LoginView

from . import views

app_name = 'interviews'

urlpatterns = [
    path('', views.home, name='home'),
    path('healthz', views.healthz, name='healthz'),
    path('schedule/', views.schedule_list, name='schedule_list'),
    path('schedule/<int:pk>/', views.schedule_detail, name='schedule_detail'),
    path('availability/<int:round_id>/', views.availability, name='availability'),
    path('rounds/<int:round_id>/responses/', views.round_responses, name='round_responses'),
    path('login/', views.login_request, name='login_request'),
    path('auth/', LoginView.as_view(), name='login_consume'),
    path('logout/', views.logout_view, name='logout'),
]
