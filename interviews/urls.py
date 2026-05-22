from django.urls import path
from sesame.views import LoginView

from . import views

app_name = 'interviews'

urlpatterns = [
    path('', views.home, name='home'),
    path('healthz', views.healthz, name='healthz'),
    path('login/', views.login_request, name='login_request'),
    path('auth/', LoginView.as_view(), name='login_consume'),
    path('logout/', views.logout_view, name='logout'),
]
