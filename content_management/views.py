from __future__ import absolute_import, unicode_literals, division, print_function

__author__ = 'reyrodrigues'

from django.http import HttpResponse, Http404
from django.template import RequestContext
from django.conf import settings
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from cms.models import Title
import email.utils
import time

from cms.utils import copy_plugins
import cms.api

from . import utils
import json

SHIM_LANGUAGE_DICTIONARY = {
    'af': 'ps'
}
"""
The Shim above is because django doesnt support Pashto, but Transifex does.
"""


def generate_blank(request, slug):
    staging = Title.objects.filter(language='en', slug='staging')
    if staging:
        staging = staging[0].page
    titles = Title.objects.filter(language='en', slug=slug, page__in=staging.get_descendants())

    if not titles:
        raise Http404

    page = titles[0].page.get_public_object()

    html = _generate_html_for_translations(titles[0], page)

    timestamp = time.mktime(page.publication_date.timetuple())

    response = HttpResponse(html, content_type="text/html")
    response['Last-Modified'] = email.utils.formatdate(timestamp)
    return response

@csrf_exempt
def validate_page(request):
    """
    Server side of web hook that receives a transition from Jira and publishes the english version of the page.
    """
    body = request.body
    if body:
        issue = json.loads(body)

        url = issue['issue']['fields'][settings.JIRA_PAGE_ADDRESS_FIELD]
        slugs = url.split('/')[2:-1]

        staging = slugs[0]
        slug = slugs[-1]

        if staging == 'staging':
            utils.promote_page.delay(slug=slug, publish=True, user_id=None, languages=['en', ])

            return push_to_transifex(request, slug)

    return HttpResponse()

@csrf_exempt
def complete_page(request):
    """
    Server side of web hook that completes the workflow by publishing translations.
    """
    body = request.body
    if body:
        issue = json.loads(body)

        url = issue['issue']['fields'][settings.JIRA_PAGE_ADDRESS_FIELD]
        slugs = url.split('/')[2:-1]

        staging = slugs[0]
        slug = slugs[-1]

        if staging == 'staging':
            utils.promote_page.delay(slug=slug, publish=True, user_id=None, languages=None)

    return HttpResponse()

def push_to_transifex(request, slug):
    staging = Title.objects.filter(language='en', slug='staging')
    if staging:
        staging = staging[0].page
    titles = Title.objects.filter(language='en', slug=slug, page__in=staging.get_descendants())

    if not titles:
        raise Http404

    page = titles[0].page

    utils.push_to_transifex.delay(page.pk)

    return render(request, "push-to-transifex.html", {}, context_instance=RequestContext(request))


def pull_from_transifex(request, slug, language):
    l = language if language not in SHIM_LANGUAGE_DICTIONARY else SHIM_LANGUAGE_DICTIONARY[language]
    utils.pull_from_transifex.delay(slug, l)

    return render(request, "promote-to-production.html", {}, context_instance=RequestContext(request))


from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def receive_translation(request):
    slug = request.POST.get('resource').lower().replace('html', '')
    language = request.POST.get('language').lower()
    project = request.POST.get('project').lower()

    import random

    utils.pull_from_transifex.apply_async(args=(slug, language, project), countdown=random.randint(10, 20))

    from project_management import utils as project_management

    project_management.transition_jira_ticket.apply_async(args=(slug, project), countdown=random.randint(10, 20))

    return HttpResponse("")


def copy_from_production(request, slug):
    staging = Title.objects.filter(language='en', slug='staging')
    production = Title.objects.filter(language='en', slug='production')
    if staging:
        staging = staging[0].page
    if production:
        production = production[0].page
    staging_title = Title.objects.filter(language='en', slug=slug, page__in=staging.get_descendants())
    production_title = Title.objects.filter(language='en', slug=slug, page__in=production.get_descendants())

    if staging_title and production_title:
        staging_title = staging_title[0]
        production_title = production_title[0]

        staging_page = staging_title.page
        production_page = production_title.page

        _duplicate_page(production_page, staging_page, True, request.user)

    return render(request, "copy-from-production.html", {}, context_instance=RequestContext(request))


def promote_to_production(request, slug):
    utils.promote_page.delay(slug=slug, publish=False, user_id=request.user.id)

    return render(request, "promote-to-production.html", {}, context_instance=RequestContext(request))


def _duplicate_page(source, destination, publish=None, user=None):
    placeholders = source.get_placeholders()

    source = source.get_public_object()
    destination = destination.get_draft_object()
    en_title = source.get_title_obj(language='en')

    destination_placeholders = dict([(a.slot, a) for a in destination.get_placeholders()])
    for k, v in settings.LANGUAGES:
        available = [a.language for a in destination.title_set.all()]
        title = source.get_title_obj(language=k)

        # Doing some cleanup while I am at it
        if en_title and title:
            title.title = en_title.title
            title.slug = en_title.slug
            if hasattr(title, 'save'):
                title.save()

        if not k in available:
            cms.api.create_title(k, title.title, destination, slug=title.slug)

        try:
            destination_title = destination.get_title_obj(language=k)
            if en_title and title and destination_title:
                destination_title.page_title = title.page_title
                destination_title.slug = en_title.slug

                if hasattr(destination_title, 'save'):
                    destination_title.save()
        except Exception as e:
            print("Error updating title.")

    for placeholder in placeholders:
        destination_placeholders[placeholder.slot].clear()

        for k, v in settings.LANGUAGES:
            plugins = list(
                placeholder.cmsplugin_set.filter(language=k).order_by('path')
            )
            copied_plugins = copy_plugins.copy_plugins_to(plugins, destination_placeholders[placeholder.slot], k)
    if publish:
        try:
            for k, v in settings.LANGUAGES:
                cms.api.publish_page(destination, user, k)
        except Exception as e:
            pass


def _generate_html_for_translations(title, page):
    messages = []
    for placeholder in page.get_placeholders():
        sort_function = lambda item: item.get_plugin_instance()[0].get_position_in_placeholder()
        plugins = sorted(placeholder.get_plugins('en'), key=sort_function)

        for plugin in plugins:
            line = {}
            instance, t = plugin.get_plugin_instance()
            line.update(id=instance.id)
            line.update(position=instance.get_position_in_placeholder())

            type_name = type(t).__name__
            line.update(type=type_name)

            if instance.get_parent():
                line.update(parent=instance.get_parent().id)
            else:
                line.update(parent='')

            if hasattr(instance, 'body'):
                line.update(text=instance.body.encode('ascii', 'xmlcharrefreplace'))
            elif hasattr(instance, 'title'):
                line.update(text=instance.title.encode('ascii', 'xmlcharrefreplace'))
            elif hasattr(instance, 'name'):
                line.update(text=instance.name.encode('ascii', 'xmlcharrefreplace'))
            else:
                line.update(text='')
            line.update(translated='')
            line['text'] = line['text'].replace('&#160;', ' ')
            messages.append(line)
    div_format = """<div data-id="{id}"
    data-position="{position}"
    data-type="{type}"
    data-parent="{parent}">{text}</div>"""
    html = "<html>"
    html += "<body>"
    html += "<div class='title'>{}</div>".format(title.page_title)
    html += '\n'.join(
        [div_format.format(**a) for a in messages]
    )
    html += "</body>"
    html += "</html>"

    return html


def _translate_page(dict_list, language, page):
    for c in page.get_placeholders():
        c.clear(language)
    cms.api.copy_plugins_to_language(page, 'en', language)
    for c in page.get_placeholders():
        for d in c.get_plugins(language):
            instance, t = d.get_plugin_instance()
            type_name = type(t).__name__
            position = instance.get_position_in_placeholder()
            translation = [a for a in dict_list if int(a['position']) == position and a['type'] == type_name]

            if translation:
                translation = translation[0]
                translation['translated_id'] = instance.id

                text = translation['translated']

                if hasattr(instance, 'body'):
                    instance.body = text
                elif hasattr(instance, 'title'):
                    instance.title = text
                elif hasattr(instance, 'name'):
                    instance.name = text
                instance.save()