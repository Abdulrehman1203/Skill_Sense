"""Pure summary statistics shared by offline acceptance and local operations."""
import math


def percentiles(values):
    values = sorted(v for v in values if v is not None and math.isfinite(v) and v >= 0)
    def at(q):
        if not values:
            return None
        index = (len(values)-1)*q
        lo = int(index)
        return round(values[lo]+(values[min(lo+1, len(values)-1)]-values[lo])*(index-lo), 3)
    return {'count': len(values), 'p50': at(.5), 'p95': at(.95)}
