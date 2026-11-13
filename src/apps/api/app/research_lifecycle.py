"""Version-chain coordination for a single research run.

The operation store owns cancellation and atomic checkpoint admission. This
boundary owns the research document's expected parent and never overwrites a
concurrent user edit or a different run's version.
"""
from __future__ import annotations

from dataclasses import dataclass

from .research_topics import (ResearchContent, ResearchTopics, ResearchVersion,
                              TopicVersionConflict, VersionInput)


@dataclass
class ResearchVersionChain:
    topics: ResearchTopics
    owner_id: str
    topic_id: str
    parent_version_id: str
    source_ids: list[str]

    def current(self) -> ResearchVersion:
        current = self.topics.get(self.owner_id, self.topic_id).versions[-1]
        if current.version_id != self.parent_version_id:
            raise TopicVersionConflict()
        return current

    def save(self, parent: ResearchVersion, content: ResearchContent) -> ResearchVersion:
        if parent.version_id != self.parent_version_id:
            raise TopicVersionConflict()
        topic = self.topics.add_version(self.owner_id, self.topic_id, VersionInput(
            parent_version_id=self.parent_version_id, content=content, note=parent.note),
            source_ids=self.source_ids, created_by='agent')
        version = topic.versions[-1]
        self.parent_version_id = version.version_id
        return version
