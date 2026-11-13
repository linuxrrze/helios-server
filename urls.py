# -*- coding: utf-8 -*-
from django.conf import settings
from django.urls import include, path, re_path
from django.views.i18n import JavaScriptCatalog
from django.views.static import serve
from helios import views as helios_views

urlpatterns = [
    path('auth/', include('helios_auth.urls')),
    path('helios/', include('helios.urls')),

    # language switcher (POST, sets the language cookie) and the JavaScript
    # translation catalog for the voting booth and verifier
    path('i18n/', include('django.conf.urls.i18n')),
    path('jsi18n/', JavaScriptCatalog.as_view(), name='javascript-catalog'),

    # SHOULD BE REPLACED BY APACHE STATIC PATH
    re_path(r'booth/(?P<path>.*)$', serve, {'document_root' : settings.ROOT_PATH + '/heliosbooth'}),
    re_path(r'verifier/(?P<path>.*)$', serve, {'document_root' : settings.ROOT_PATH + '/heliosverifier'}),

    # site-specific files (logos, stylesheets), see SITE_STATIC_DIR
    *([re_path(r'static/site/(?P<path>.*)$', serve, {'document_root': settings.SITE_STATIC_DIR})]
      if settings.SITE_STATIC_DIR else []),
    re_path(r'static/auth/(?P<path>.*)$', serve, {'document_root' : settings.ROOT_PATH + '/helios_auth/media'}),
    re_path(r'static/helios/(?P<path>.*)$', serve, {'document_root' : settings.ROOT_PATH + '/helios/media'}),
    re_path(r'static/(?P<path>.*)$', serve, {'document_root' : settings.ROOT_PATH + '/server_ui/media'}),

    # Email opt-out/opt-in URLs
    path('optout/', include([
        path('', helios_views.optout_form, name='optout_form'),
        path('success/', helios_views.optout_success, name='optout_success'),
        path('confirm/<str:email>/<str:code>/', helios_views.optout_confirm, name='optout_confirm'),
    ])),
    path('optin/', include([
        path('', helios_views.optin_form, name='optin_form'),
        path('success/', helios_views.optin_success, name='optin_success'),
        path('confirm/<str:email>/<str:code>/', helios_views.optin_confirm, name='optin_confirm'),
    ])),

    path('', include('server_ui.urls')),
]

# SAML assertion consumer service under a custom path, e.g. one that is already
# registered with the IdP (e.g. /saml2_auth/acs/ from the former django-saml2-auth)
if settings.SAML_SP_ACS_URL:
    from urllib.parse import urlparse
    _acs_path = urlparse(settings.SAML_SP_ACS_URL).path.lstrip('/')
    if _acs_path and _acs_path != 'auth/saml/acs':
        from helios_auth.auth_systems import saml as _saml
        urlpatterns.insert(0, path(_acs_path, _saml.acs))
