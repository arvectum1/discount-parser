from arvectum_data import (
    AcquisitionError,
    ExtractionJob,
    FieldSpec,
    JobExecutor,
    JobItemStatus,
    JsonJobCheckpointStore,
    RetryPolicy,
)


class FailingPipeline:
    def extract(self, request, fields):
        raise AcquisitionError(
            f"failed for {request.url}?token=secret"
        )


def test_failed_checkpoint_redacts_url_from_error_summary(tmp_path):
    directory = tmp_path / "jobs"
    executor = JobExecutor(
        FailingPipeline(),
        retry_policy=RetryPolicy(max_attempts=1),
        checkpoint_store=JsonJobCheckpointStore(directory),
    )
    job = ExtractionJob.from_urls(
        "redaction-job",
        ["https://a.test/private"],
        [FieldSpec("price")],
    )

    result = executor.run(job)

    assert result.items[0].status is JobItemStatus.FAILED
    text = next(directory.iterdir()).read_text(encoding="utf-8")
    assert "https://a.test" not in text
    assert "<url>" in text
