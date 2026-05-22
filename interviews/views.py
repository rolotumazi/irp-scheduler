from django.conf import settings
from django.contrib.auth import logout
from django.core.mail import send_mail
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from sesame.utils import get_query_string

from .forms import LoginRequestForm
from .models import User


def healthz(request):
    """Liveness probe for the compose healthcheck and post-deploy smoke test."""
    return HttpResponse('ok', content_type='text/plain')


def home(request):
    return render(request, 'interviews/home.html')


def login_request(request):
    if request.user.is_authenticated:
        return redirect('interviews:home')

    if request.method == 'POST':
        form = LoginRequestForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email'].lower().strip()
            user = User.objects.filter(email__iexact=email, is_active=True).first()
            if user is not None:
                _send_magic_link(user, request)
            # Always show the same confirmation — avoid leaking which emails exist.
            return render(request, 'interviews/login_sent.html', {'email': email})
    else:
        form = LoginRequestForm()

    return render(request, 'interviews/login_request.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('interviews:home')


def _send_magic_link(user, request):
    query_string = get_query_string(user)
    path = reverse('interviews:login_consume')
    link = f'{settings.SITE_URL}{path}{query_string}'

    send_mail(
        subject='Your Schedule IRP sign-in link',
        message=(
            f'Hello {user.get_full_name() or user.email},\n\n'
            f'Click the link below to sign in. It expires in 15 minutes and can only be used once.\n\n'
            f'{link}\n\n'
            f'If you did not request this link, you can safely ignore this email.\n'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )
