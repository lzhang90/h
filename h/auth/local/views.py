# -*- coding: utf-8 -*-
import colander
import deform
import horus.views
from horus.lib import FlashMessage
from pyramid import httpexceptions
from pyramid.view import view_config, view_defaults

from h import events, views
from h.auth.local import forms, models, schemas
from h.models import _


def ajax_form(request, result):
    flash = views.pop_flash(request)

    if isinstance(result, httpexceptions.HTTPRedirection):
        request.response.headers.extend(result.headers)
        result = {'status': 'okay'}
    elif isinstance(result, httpexceptions.HTTPError):
        request.response.status_code = result.code
        result = {'status': 'failure', 'reason': str(result)}
    else:
        errors = result.pop('errors', [])
        if errors:
            request.response.status_code = 400
            result['status'] = 'failure'

            for e in errors:
                if isinstance(e, colander.Invalid):
                    result.setdefault('errors', {})
                    result['errors'].update(e.asdict())

        reasons = flash.pop('error', [])
        if reasons:
            assert(len(reasons) == 1)
            request.response.status_code = 400
            result['status'] = 'failure'
            result['reason'] = reasons[0]

    result['flash'] = flash

    # We do this here rather than in a subscriber because horus will fire
    # events before calling :func:`pyramid.security.remember`. We want to
    # correct the state of things after that.
    if hasattr(request.user, 'id'):
        user = 'acct:%s@%s' % (request.user.username, request.server_name)
        event = events.LoginEvent(request, user)
        request.registry.notify(event)

    return result


class AsyncFormViewMapper(object):
    def __init__(self, **kw):
        self.attr = kw['attr']

    def __call__(self, view):
        def wrapper(context, request):
            if request.method == 'POST':
                data = request.json_body
                data.update(request.params)
                request.content_type = 'application/x-www-form-urlencoded'
                request.POST.clear()
                request.POST.update(data)
            inst = view(request)
            meth = getattr(inst, self.attr)
            result = meth()
            result = ajax_form(request, result)
            result['model'] = views.model(request)
            result.pop('form', None)
            return result
        return wrapper


@view_defaults(accept='text/html', renderer='h:templates/auth.pt')
@view_config(attr='login', route_name='login')
@view_config(attr='logout', route_name='logout')
class AuthController(horus.views.AuthController):
    def login(self):
        request = self.request
        result = super(AuthController, self).login()

        if request.user:
            # XXX: Horus should maybe do this for us
            user = 'acct:%s@%s' % (request.user.username, request.server_name)
            event = events.LoginEvent(request, user)
            request.registry.notify(event)

        return result

    def logout(self):
        request = self.request
        result = super(AuthController, self).logout()

        # XXX: Horus should maybe do this for us
        event = events.LogoutEvent(request)
        request.registry.notify(event)

        return result


@view_defaults(accept='application/json', name='app', renderer='json')
@view_config(attr='login', request_param='__formid__=login')
@view_config(attr='logout', request_param='__formid__=logout')
class AsyncAuthController(AuthController):
    __view_mapper__ = AsyncFormViewMapper


@view_defaults(accept='text/html', renderer='h:templates/auth.pt')
@view_config(attr='forgot_password', route_name='forgot_password')
@view_config(attr='reset_password', route_name='reset_password')
class ForgotPasswordController(horus.views.ForgotPasswordController):
    pass


@view_defaults(accept='application/json', name='app', renderer='json')
@view_config(attr='forgot_password', request_param='__formid__=forgot')
class AsyncForgotPasswordController(ForgotPasswordController):
    __view_mapper__ = AsyncFormViewMapper


@view_defaults(accept='text/html', renderer='h:templates/auth.pt')
@view_config(attr='register', route_name='register')
@view_config(attr='activate', route_name='activate')
class RegisterController(horus.views.RegisterController):
    pass


@view_defaults(accept='application/json', name='app', renderer='json')
@view_config(attr='register', request_param='__formid__=register')
@view_config(attr='activate', request_param='__formid__=activate')
class AsyncRegisterController(RegisterController):
    __view_mapper__ = AsyncFormViewMapper

    def activate(self):
        """Activate a user and set a password given an activation code.

        This view is different from the activation view in horus because it
        does not require the user id to be passed. It trusts the activation
        code and updates the password.
        """
        request = self.request
        Str = self.Str

        schema = schemas.ActivationSchema.bind(request=request)
        form = forms.ActivateForm(schema)
        appstruct = None

        try:
            appstruct = form.validate(request.POST.items())
        except deform.ValidationFailure as e:
            return dict(errors=e.error.children)

        code = appstruct['code']
        activation = models.Activation.get_by_code(request, code)

        user = None
        if activation:
            user = self.User.get_by_activation(request, activation)

        if user is None:
            return dict(errors=[_('This activation code is not valid.')])

        user.password = appstruct['password']
        self.db.delete(activation)
        self.db.add(user)

        FlashMessage(request, Str.reset_password_done, kind='success')

        # XXX: Horus should maybe do this for us
        event = events.RegistrationActivatedEvent(request, user, activation)
        request.registry.notify(event)

        return {}


def includeme(config):
    registry = config.registry
    settings = registry.settings

    authz_endpoint = settings.get('auth.local.authorize', '/oauth/authorize')
    config.add_route('auth.local.authorize', authz_endpoint)

    token_endpoint = settings.get('auth.local.token', '/oauth/token')
    config.add_route('auth.local.token', token_endpoint)

    config.include('horus')
    config.scan(__name__)
