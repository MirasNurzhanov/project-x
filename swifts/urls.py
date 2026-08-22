from django.urls import path

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
    SwiftMusicProviderStatusView,
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
    path('mine/', SwiftMineView.as_view(), name='swift-mine'),

    # Comment deletion is keyed on the comment, not the Swift, so it sits above
    # the `<int:pk>/` patterns — otherwise `comments/<id>/` would be read as a
    # Swift called "comments".
    path('comments/<int:comment_id>/', SwiftCommentDetailView.as_view(),
         name='swift-comment-delete'),

    path('<int:pk>/like/', SwiftLikeView.as_view(), name='swift-like'),
    path('<int:pk>/repost/', SwiftRepostView.as_view(), name='swift-repost'),
    path('<int:pk>/comments/', SwiftCommentsView.as_view(), name='swift-comments'),
    path('<int:pk>/view/', RecordSwiftViewView.as_view(), name='swift-view'),
    path('<int:pk>/report/', SwiftReportView.as_view(), name='swift-report'),

    # The whole location surface. `reveal-location/` is the only route in the
    # project that can answer with a Swift's coordinate; `reveals/` is the
    # creator's audit of who has asked.
    path('<int:pk>/reveal-location/', RevealSwiftLocationView.as_view(),
         name='swift-reveal-location'),
    path('<int:pk>/reveals/', SwiftLocationRevealAuditView.as_view(),
         name='swift-reveal-audit'),

    path('<int:pk>/', SwiftDetailView.as_view(), name='swift-detail'),
]
