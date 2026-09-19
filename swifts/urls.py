from django.urls import path, re_path
from .creator import SwiftAnalyticsView, SwiftCreatorView

from .views import (
    RecordSwiftViewView,
    RevealSwiftLocationView,
    SwiftCommentDetailView,
    SwiftCommentsView,
    SwiftDetailView,
    SwiftFeedView,
    SwiftLikeView,
    SwiftListCreateView,
    SwiftLocationRevealAuditView,
    SwiftMineView,
    SwiftReportView,
    SwiftRepostView,
    SwiftSavedView,
    SwiftSaveView,
    SwiftHashtagFeedView,
    SwiftHashtagSuggestView,
    SwiftMusicProviderStatusView,
    SwiftSendView,
    SwiftTrackGenresView,
    SwiftTrackListView,
)

urlpatterns = [
    path('', SwiftListCreateView.as_view(), name='swift-create'),
    # Two lists, two rules. `feed/` is everybody else's Swifts under the
    # audience gate; `mine/` is the only surface that shows a creator a row the
    # feed would withhold — their own private and still-processing ones.
    path('feed/', SwiftFeedView.as_view(), name='swift-feed'),
    # The composer's supporting endpoints. Above `<int:pk>/` so neither path is
    # read as a Swift id. There is no upload-signature route any more — the
    # video goes straight into the POST below as a normal multipart file.
    path('tracks/', SwiftTrackListView.as_view(), name='swift-tracks'),
    path('tracks/genres/', SwiftTrackGenresView.as_view(), name='swift-track-genres'),
    path('tracks/providers/', SwiftMusicProviderStatusView.as_view(),
         name='swift-music-providers'),
    # Tag suggestions for the publishing step. Above `<int:pk>/` for the same
    # reason every other composer route is.
    path('hashtags/', SwiftHashtagSuggestView.as_view(), name='swift-hashtags'),
    # A tag's own page. `[^/]+` rather than `<str:name>` so a tag containing a
    # dot or a hyphen still resolves — `str` matches neither reliably once the
    # tag is URL-encoded.
    re_path(r'^hashtags/(?P<name>[^/]+)/$', SwiftHashtagFeedView.as_view(),
            name='swift-hashtag-feed'),
    path('mine/', SwiftMineView.as_view(), name='swift-mine'),
    # The viewer's own shortlist. Above `<int:pk>/` so "saved" is not read
    # as a Swift id, same reason as `mine/` and `tracks/`.
    path('saved/', SwiftSavedView.as_view(), name='swift-saved'),
    path('creator/<int:user_id>/', SwiftCreatorView.as_view(), name='swift-creator'),
    path('analytics/', SwiftAnalyticsView.as_view(), name='swift-analytics'),

    # Comment deletion is keyed on the comment, not the Swift, so it sits above
    # the `<int:pk>/` patterns — otherwise `comments/<id>/` would be read as a
    # Swift called "comments".
    path('comments/<int:comment_id>/', SwiftCommentDetailView.as_view(),
         name='swift-comment-delete'),

    path('<int:pk>/like/', SwiftLikeView.as_view(), name='swift-like'),
    path('<int:pk>/repost/', SwiftRepostView.as_view(), name='swift-repost'),
    path('<int:pk>/save/', SwiftSaveView.as_view(), name='swift-save'),
    path('<int:pk>/comments/', SwiftCommentsView.as_view(), name='swift-comments'),
    path('<int:pk>/view/', RecordSwiftViewView.as_view(), name='swift-view'),
    path('<int:pk>/report/', SwiftReportView.as_view(), name='swift-report'),
    # Forwarding: a Swift into somebody's DMs. Lives here rather than under
    # `friends/` because the gate that matters is the Swift's audience, and
    # this is where that gate is.
    path('<int:pk>/send/', SwiftSendView.as_view(), name='swift-send'),

    # The whole location surface. `reveal-location/` is the only route in the
    # project that can answer with a Swift's coordinate; `reveals/` is the
    # creator's audit of who has asked.
    path('<int:pk>/reveal-location/', RevealSwiftLocationView.as_view(),
         name='swift-reveal-location'),
    path('<int:pk>/reveals/', SwiftLocationRevealAuditView.as_view(),
         name='swift-reveal-audit'),

    path('<int:pk>/', SwiftDetailView.as_view(), name='swift-detail'),
]
