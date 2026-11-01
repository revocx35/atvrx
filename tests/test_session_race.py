import threading

from atvrx.session import Config, Session


def test_two_concurrent_starts_leave_one_stoppable_worker(tmp_path):
    """Two Watch/Scan requests at once (double click; FastAPI runs them in parallel threads)."""
    sess = Session(Config(), tmp_path)
    workers = []

    def target():
        me = threading.current_thread()
        workers.append(me)
        me._args[1].wait(5)              # the stop event this worker was given

    real_stop = sess.stop
    barrier = threading.Barrier(2, timeout=0.5)

    def stop(message=""):
        real_stop(message)
        try:
            barrier.wait()               # let the other request run its stop() too before going on
        except threading.BrokenBarrierError:
            pass

    sess.stop = stop
    callers = [threading.Thread(target=sess._run, args=(target,)) for _ in range(2)]
    for t in callers:
        t.start()
    for t in callers:
        t.join()
    sess.stop = real_stop
    sess.stop()
    for w in workers:
        w.join(timeout=1)
    assert not any(w.is_alive() for w in workers), "a worker kept the radio and can no longer be stopped"
