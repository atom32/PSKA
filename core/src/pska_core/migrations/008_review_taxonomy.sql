ALTER TYPE pska_review_type ADD VALUE IF NOT EXISTS 'memory_candidate';
ALTER TYPE pska_review_type ADD VALUE IF NOT EXISTS 'relationship_candidate';
ALTER TYPE pska_review_type ADD VALUE IF NOT EXISTS 'action_candidate';
ALTER TYPE pska_review_type ADD VALUE IF NOT EXISTS 'low_confidence';
