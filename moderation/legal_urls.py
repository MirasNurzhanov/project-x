from django.urls import path

from .legal_views import guidelines_page, privacy_page, terms_page

urlpatterns = [
    path('terms/', terms_page, name='legal-terms'),
    path('guidelines/', guidelines_page, name='legal-guidelines'),
    path('privacy/', privacy_page, name='legal-privacy'),
]
