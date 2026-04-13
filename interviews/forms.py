from django import forms


class LoginRequestForm(forms.Form):
    email = forms.EmailField(
        label='Email address',
        widget=forms.EmailInput(attrs={
            'autocomplete': 'email',
            'autofocus': True,
            'placeholder': 'you@example.ac.uk',
        }),
    )
