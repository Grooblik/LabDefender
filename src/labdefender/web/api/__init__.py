"""JSON API of the Mini App. Every /api/ route is authenticated by the middleware in web.app."""

from aiohttp import web

from labdefender.web.api import courses, defense, notifications, profile, submissions


def add_routes(app: web.Application) -> None:
    for module in (profile, courses, defense, submissions, notifications):
        app.add_routes(module.routes)
