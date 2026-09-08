from clipwatch.stability import wait_until_stable


class FakeClock:
    """Drives sleep and monotonic together so tests never actually wait."""

    def __init__(self):
        self.t = 0.0

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return self.t


def test_returns_true_when_the_file_stops_growing(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x" * 10)
    clock = FakeClock()
    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=30,
                             sleep=clock.sleep, now=clock.now) is True


def test_waits_while_the_file_is_still_growing(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    clock = FakeClock()
    grew = {"n": 0}

    def growing_sleep(seconds):
        clock.sleep(seconds)
        if grew["n"] < 4:
            grew["n"] += 1
            f.write_bytes(b"x" * (10 * grew["n"]))

    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=30,
                             sleep=growing_sleep, now=clock.now) is True
    assert grew["n"] == 4  # it kept waiting while the size changed


def test_times_out_on_a_file_that_never_settles(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    clock = FakeClock()
    size = {"n": 1}

    def always_growing(seconds):
        clock.sleep(seconds)
        size["n"] += 1
        f.write_bytes(b"x" * size["n"])

    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=5,
                             sleep=always_growing, now=clock.now) is False


def test_returns_false_when_the_file_disappears(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    clock = FakeClock()

    def deleting_sleep(seconds):
        clock.sleep(seconds)
        f.unlink(missing_ok=True)

    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=30,
                             sleep=deleting_sleep, now=clock.now) is False


def test_returns_false_for_a_file_that_never_existed(tmp_path):
    clock = FakeClock()
    assert wait_until_stable(tmp_path / "nope.mkv", checks=3, interval_s=0.5,
                             timeout_s=5, sleep=clock.sleep, now=clock.now) is False


def test_a_zero_byte_file_is_not_considered_stable(tmp_path):
    # OBS creates the file before writing to it.
    f = tmp_path / "a.mkv"
    f.write_bytes(b"")
    clock = FakeClock()
    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=5,
                             sleep=clock.sleep, now=clock.now) is False
