"""
Library mathematical operations
"""
import bisect
import math


def format_ratio(numerator, denominator, num_digits=None, resolve_nan=None):
    """
    Convenience function that converts two numbers to a ratio.
    Handles dividing by zero, as well as transforming values into floats.
    Rounds the number to the number of num_digits, if requested (not None)
    resolve_nan defines what to do when dividing by zero. Default is to return float('nan'), but this can be changed.
    """
    if denominator == 0 or math.isnan(denominator) or math.isnan(numerator):
        if resolve_nan is None:
            return float('nan')
        else:
            return resolve_nan
    r = float(numerator) / float(denominator)
    if num_digits is not None:
        r = round(r, num_digits)
    return r


def find_closest(numeric_list, query_number):
    """
    Given a list of numbers, and a single query number, find the number in the sorted list that is numerically
    closest to the query number. Uses list bisection to do so, and so should be O(log n)
    """
    sorted_numeric_list = sorted(numeric_list)
    pos = bisect.bisect_left(sorted_numeric_list, query_number)
    if pos == 0:
        return sorted_numeric_list[0]
    if pos == len(sorted_numeric_list):
        return sorted_numeric_list[-1]
    before = sorted_numeric_list[pos - 1]
    after = sorted_numeric_list[pos]
    if after - query_number < query_number - before:
        return after
    else:
        return before
